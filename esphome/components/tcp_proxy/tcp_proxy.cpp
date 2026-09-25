#include "tcp_proxy.h"

#ifdef USE_TCP_PROXY

#include <cerrno>
#include <cinttypes>
#include <cstring>
#include <new>

#include "esphome/components/api/api_connection.h"
#include "esphome/core/application.h"
#include "esphome/core/hal.h"
#include "esphome/core/log.h"

#ifdef USE_SOCKET_IMPL_LWIP_SOCKETS
#include <lwip/sockets.h>
#else
#include <sys/select.h>
#endif

namespace esphome::tcp_proxy {

static const char *const TAG = "tcp_proxy";

/// Socket reads per stream per loop iteration, so one busy stream cannot starve the loop
static constexpr uint8_t MAX_READS_PER_LOOP = 4;

TCPProxy *global_tcp_proxy = nullptr;  // NOLINT(cppcoreguidelines-avoid-non-const-global-variables)

TCPProxy::TCPProxy() { global_tcp_proxy = this; }

void TCPProxy::add_target(const char *name, const char *address, uint16_t port, api::enums::TcpProxyTargetType type,
                          const char *path) {
  if (this->target_count_ >= this->targets_.size())
    return;
  this->targets_[this->target_count_++] = TCPProxyTarget{name, address, path, port, type};
}

void TCPProxy::setup() {
  this->streams_ = std::make_unique<TCPProxyStream[]>(this->max_connections_);
  this->disable_loop();
}

void TCPProxy::dump_config() {
  ESP_LOGCONFIG(TAG,
                "TCP Proxy:\n"
                "  Max connections: %u\n"
                "  Buffer size: %u\n"
                "  Connect timeout: %" PRIu32 " ms",
                this->max_connections_, this->buffer_size_, this->connect_timeout_);
  for (uint8_t i = 0; i < this->target_count_; i++) {
    const auto &target = this->targets_[i];
    ESP_LOGCONFIG(
        TAG, "  Target %u: '%s' -> %s:%u (%s, path %s)", i, target.name, target.address, target.port,
        target.type == api::enums::TCP_PROXY_TARGET_TYPE_HTTP ? LOG_STR_LITERAL("http") : LOG_STR_LITERAL("raw"),
        target.path);
  }
}

TCPProxyStream *TCPProxy::find_stream_(api::APIConnection *conn, uint32_t stream_id) {
  for (uint8_t i = 0; i < this->max_connections_; i++) {
    auto &stream = this->streams_[i];
    if (stream.state != StreamState::FREE && stream.conn == conn && stream.stream_id == stream_id)
      return &stream;
  }
  return nullptr;
}

void TCPProxy::reject_(api::APIConnection *conn, uint32_t stream_id, api::enums::TcpProxyStatus status) {
  api::TcpProxyOpenResponse resp;
  resp.stream_id = stream_id;
  resp.status = status;
  if (!conn->send_message(resp)) {
    ESP_LOGW(TAG, "Could not reject stream %" PRIu32 ", API buffer full", stream_id);
  }
}

void TCPProxy::on_open_request(api::APIConnection *conn, const api::TcpProxyOpenRequest &msg) {
  if (msg.target >= this->target_count_ || this->find_stream_(conn, msg.stream_id) != nullptr) {
    this->reject_(conn, msg.stream_id, api::enums::TCP_PROXY_STATUS_INVALID_ARGUMENT);
    return;
  }
  TCPProxyStream *stream = nullptr;
  for (uint8_t i = 0; i < this->max_connections_; i++) {
    if (this->streams_[i].state == StreamState::FREE) {
      stream = &this->streams_[i];
      break;
    }
  }
  if (stream == nullptr) {
    ESP_LOGW(TAG, "No free stream slot for stream %" PRIu32, msg.stream_id);
    this->reject_(conn, msg.stream_id, api::enums::TCP_PROXY_STATUS_NO_RESOURCES);
    return;
  }

  const auto &target = this->targets_[msg.target];
  struct sockaddr_storage addr;
  socklen_t addrlen =
      socket::set_sockaddr(reinterpret_cast<struct sockaddr *>(&addr), sizeof(addr), target.address, target.port);
  auto sock = addrlen == 0 ? nullptr : socket::socket(addr.ss_family, SOCK_STREAM, 0);
  std::unique_ptr<uint8_t[]> rx_buf(new (std::nothrow) uint8_t[this->buffer_size_]);
  if (this->read_buf_ == nullptr)
    this->read_buf_.reset(new (std::nothrow) uint8_t[this->buffer_size_]);
  if (sock == nullptr || rx_buf == nullptr || this->read_buf_ == nullptr) {
    this->reject_(conn, msg.stream_id, api::enums::TCP_PROXY_STATUS_NO_RESOURCES);
    return;
  }
  sock->setblocking(false);
  int one = 1;
  sock->setsockopt(IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));

  int err = sock->connect(reinterpret_cast<struct sockaddr *>(&addr), addrlen);
  if (err != 0 && errno != EINPROGRESS) {
    ESP_LOGW(TAG, "Connecting to '%s' failed: errno %d", target.name, errno);
    this->reject_(conn, msg.stream_id, api::enums::TCP_PROXY_STATUS_CONNECT_FAILED);
    return;
  }

  stream->socket = std::move(sock);
  stream->rx_buf = std::move(rx_buf);
  stream->conn = conn;
  stream->stream_id = msg.stream_id;
  stream->started_at = App.get_loop_component_start_time();
  stream->send_window = msg.window != 0 ? msg.window : this->buffer_size_;
  stream->unacked = 0;
  stream->rx_head = 0;
  stream->rx_len = 0;
  stream->acked = false;
  stream->status = api::enums::TCP_PROXY_STATUS_OK;
  stream->state = err == 0 ? StreamState::OPEN : StreamState::CONNECTING;
  if (this->active_++ == 0) {
    this->high_freq_.start();
    this->enable_loop();
  }
  ESP_LOGD(TAG, "Stream %" PRIu32 " opening to '%s'", msg.stream_id, target.name);
}

void TCPProxy::on_data(api::APIConnection *conn, const api::TcpProxyData &msg) {
  auto *stream = this->find_stream_(conn, msg.stream_id);
  if (stream == nullptr || stream->state != StreamState::OPEN || !stream->acked) {
    // Normal right after either side closed; data may already have been in flight
    ESP_LOGV(TAG, "Dropping %u bytes for stream %" PRIu32 " that is not open", msg.data_len, msg.stream_id);
    return;
  }
  if (static_cast<uint32_t>(stream->rx_len) + msg.data_len > this->buffer_size_) {
    ESP_LOGW(TAG, "Stream %" PRIu32 " overran its window", msg.stream_id);
    this->begin_close_(*stream, api::enums::TCP_PROXY_STATUS_FLOW_CONTROL);
    return;
  }
  if (stream->rx_head + stream->rx_len + msg.data_len > this->buffer_size_) {
    memmove(stream->rx_buf.get(), stream->rx_buf.get() + stream->rx_head, stream->rx_len);
    stream->rx_head = 0;
  }
  memcpy(stream->rx_buf.get() + stream->rx_head + stream->rx_len, msg.data, msg.data_len);
  stream->rx_len += msg.data_len;
  this->flush_rx_(*stream);
  this->maybe_send_window_update_(*stream);
}

void TCPProxy::on_window_update(api::APIConnection *conn, const api::TcpProxyWindowUpdate &msg) {
  auto *stream = this->find_stream_(conn, msg.stream_id);
  if (stream == nullptr)
    return;
  // Saturate rather than wrap if a client over-credits
  uint32_t window = stream->send_window + msg.increment;
  stream->send_window = window < stream->send_window ? UINT32_MAX : window;
}

void TCPProxy::on_close(api::APIConnection *conn, const api::TcpProxyClose &msg) {
  auto *stream = this->find_stream_(conn, msg.stream_id);
  if (stream == nullptr)
    return;
  if (stream->state == StreamState::OPEN)
    this->flush_rx_(*stream);  // Best effort: deliver what the client sent before closing
  ESP_LOGD(TAG, "Stream %" PRIu32 " closed by client", msg.stream_id);
  this->release_(*stream);
}

void TCPProxy::on_connection_closed(api::APIConnection *conn) {
  for (uint8_t i = 0; i < this->max_connections_; i++) {
    if (this->streams_[i].state != StreamState::FREE && this->streams_[i].conn == conn)
      this->release_(this->streams_[i]);
  }
}

void TCPProxy::loop() {
  for (uint8_t i = 0; i < this->max_connections_; i++) {
    auto &stream = this->streams_[i];
    if (stream.state == StreamState::FREE)
      continue;
    if (stream.conn->is_marked_for_removal()) {
      this->release_(stream);
      continue;
    }
    switch (stream.state) {
      case StreamState::CONNECTING:
        this->check_connect_(stream);
        break;
      case StreamState::REJECTING:
        if (this->send_open_response_(stream, stream.status))
          this->release_(stream);
        break;
      case StreamState::CLOSING: {
        api::TcpProxyClose msg;
        msg.stream_id = stream.stream_id;
        msg.status = stream.status;
        if (stream.conn->send_message(msg))
          this->release_(stream);
        break;
      }
      case StreamState::OPEN:
        if (!stream.acked) {
          if (!this->send_open_response_(stream, api::enums::TCP_PROXY_STATUS_OK))
            break;
          stream.acked = true;
          ESP_LOGD(TAG, "Stream %" PRIu32 " open", stream.stream_id);
        }
        this->flush_rx_(stream);
        this->maybe_send_window_update_(stream);
        this->pump_tx_(stream);
        break;
      case StreamState::FREE:
        break;
    }
  }
}

bool TCPProxy::send_open_response_(TCPProxyStream &stream, api::enums::TcpProxyStatus status) {
  api::TcpProxyOpenResponse resp;
  resp.stream_id = stream.stream_id;
  resp.status = status;
  if (status == api::enums::TCP_PROXY_STATUS_OK) {
    resp.window = this->buffer_size_;
    resp.max_data_size = this->buffer_size_;
  }
  return stream.conn->send_message(resp);
}

void TCPProxy::check_connect_(TCPProxyStream &stream) {
  // A non-blocking connect() has finished once the socket turns writable; SO_ERROR then
  // says whether it worked. getpeername() cannot be used instead: lwIP reports the peer
  // as soon as the SYN is sent.
  int fd = stream.socket->get_fd();
  fd_set wfds;
  FD_ZERO(&wfds);
  FD_SET(fd, &wfds);
  struct timeval tv = {0, 0};
#ifdef USE_SOCKET_IMPL_LWIP_SOCKETS
  int ready = lwip_select(fd + 1, nullptr, &wfds, nullptr, &tv);
#else
  int ready = ::select(fd + 1, nullptr, &wfds, nullptr, &tv);
#endif
  if (ready == 0) {
    if (App.get_loop_component_start_time() - stream.started_at < this->connect_timeout_)
      return;
    ESP_LOGW(TAG, "Stream %" PRIu32 " connect timed out", stream.stream_id);
  } else {
    int so_error = 0;
    socklen_t len = sizeof(so_error);
    if (ready > 0 && stream.socket->getsockopt(SOL_SOCKET, SO_ERROR, &so_error, &len) == 0 && so_error == 0) {
      stream.state = StreamState::OPEN;
      return;
    }
    ESP_LOGW(TAG, "Stream %" PRIu32 " connect failed: errno %d", stream.stream_id, ready < 0 ? errno : so_error);
  }
  stream.socket = nullptr;
  stream.status = api::enums::TCP_PROXY_STATUS_CONNECT_FAILED;
  stream.state = StreamState::REJECTING;
}

void TCPProxy::flush_rx_(TCPProxyStream &stream) {
  while (stream.rx_len > 0) {
    ssize_t written = stream.socket->write(stream.rx_buf.get() + stream.rx_head, stream.rx_len);
    if (written > 0) {
      stream.rx_head += written;
      stream.rx_len -= written;
      stream.unacked += written;
    } else if (written < 0 && (errno == EWOULDBLOCK || errno == EAGAIN)) {
      return;
    } else {
      ESP_LOGW(TAG, "Stream %" PRIu32 " write failed: errno %d", stream.stream_id, errno);
      this->begin_close_(stream, api::enums::TCP_PROXY_STATUS_ERROR);
      return;
    }
  }
  stream.rx_head = 0;
}

void TCPProxy::maybe_send_window_update_(TCPProxyStream &stream) {
  if (stream.state != StreamState::OPEN || stream.unacked == 0)
    return;
  // Credit back once half the window is consumed, or as soon as the buffer drains so a
  // client waiting on a small remainder never stalls
  if (stream.rx_len != 0 && stream.unacked < this->buffer_size_ / 2u)
    return;
  api::TcpProxyWindowUpdate msg;
  msg.stream_id = stream.stream_id;
  msg.increment = stream.unacked;
  if (stream.conn->send_message(msg))
    stream.unacked = 0;
}

void TCPProxy::pump_tx_(TCPProxyStream &stream) {
  api::TcpProxyData msg;
  msg.stream_id = stream.stream_id;
  for (uint8_t i = 0; i < MAX_READS_PER_LOOP; i++) {
    if (stream.state != StreamState::OPEN || stream.send_window == 0)
      return;
    // Only read what we can hand off right now. Leaving data in the socket lets TCP
    // push back on the target instead of buffering it here.
    if (!stream.conn->try_to_clear_buffer(false))
      return;
    size_t want = std::min<size_t>(stream.send_window, this->buffer_size_);
    ssize_t got = stream.socket->read(this->read_buf_.get(), want);
    if (got > 0) {
      msg.data = this->read_buf_.get();
      msg.data_len = static_cast<uint16_t>(got);
      if (!stream.conn->send_message(msg)) {
        // Cannot happen after try_to_clear_buffer() unless the connection failed; the bytes
        // are gone either way, so the stream cannot continue
        this->begin_close_(stream, api::enums::TCP_PROXY_STATUS_ERROR);
        return;
      }
      stream.send_window -= got;
    } else if (got == 0) {
      ESP_LOGD(TAG, "Stream %" PRIu32 " closed by target", stream.stream_id);
      this->begin_close_(stream, api::enums::TCP_PROXY_STATUS_OK);
      return;
    } else if (errno == EWOULDBLOCK || errno == EAGAIN) {
      return;
    } else {
      ESP_LOGW(TAG, "Stream %" PRIu32 " read failed: errno %d", stream.stream_id, errno);
      this->begin_close_(stream, api::enums::TCP_PROXY_STATUS_ERROR);
      return;
    }
  }
}

void TCPProxy::begin_close_(TCPProxyStream &stream, api::enums::TcpProxyStatus status) {
  stream.socket = nullptr;
  stream.rx_buf = nullptr;
  stream.rx_len = 0;
  stream.status = status;
  stream.state = StreamState::CLOSING;
}

void TCPProxy::release_(TCPProxyStream &stream) {
  stream.socket = nullptr;
  stream.rx_buf = nullptr;
  stream.conn = nullptr;
  stream.state = StreamState::FREE;
  if (--this->active_ == 0) {
    this->read_buf_ = nullptr;
    this->high_freq_.stop();
    this->disable_loop();
  }
}

}  // namespace esphome::tcp_proxy

#endif  // USE_TCP_PROXY
