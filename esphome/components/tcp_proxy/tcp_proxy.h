#pragma once

// WARNING: This component is a PROOF OF CONCEPT. The API may change at any time.

#include "esphome/core/defines.h"

#ifdef USE_TCP_PROXY

#include <array>
#include <memory>

#include "esphome/core/component.h"
#include "esphome/core/helpers.h"
#include "esphome/components/api/api_pb2.h"
#include "esphome/components/socket/socket.h"

namespace esphome::api {
class APIConnection;
}  // namespace esphome::api

namespace esphome::tcp_proxy {

/// An endpoint the device relays streams to. Fixed at compile time: clients pick a
/// target by index and never supply an address.
struct TCPProxyTarget {
  const char *name;
  const char *address;
  const char *path;
  uint16_t port;
  api::enums::TcpProxyTargetType type;
};

enum class StreamState : uint8_t {
  FREE,        ///< Slot unused
  CONNECTING,  ///< Waiting for the non-blocking connect() to finish
  OPEN,        ///< Relaying data; `acked` tells whether the open response went out
  REJECTING,   ///< Connect failed; an open response carrying `status` is still owed
  CLOSING,     ///< Socket gone; a close message carrying `status` is still owed
};

struct TCPProxyStream {
  std::unique_ptr<socket::Socket> socket;
  /// Client data not yet written to the socket. Never exceeds the buffer size because the
  /// client may only have that many uncredited bytes in flight.
  std::unique_ptr<uint8_t[]> rx_buf;
  api::APIConnection *conn{nullptr};
  uint32_t stream_id{0};
  uint32_t started_at{0};
  uint32_t send_window{0};  ///< Bytes we may still send before the client credits more
  uint32_t unacked{0};      ///< Bytes written to the socket but not yet credited to the client
  uint16_t rx_head{0};
  uint16_t rx_len{0};
  StreamState state{StreamState::FREE};
  api::enums::TcpProxyStatus status{api::enums::TCP_PROXY_STATUS_OK};
  bool acked{false};
};

class TCPProxy final : public Component {
 public:
  TCPProxy();

  void setup() override;
  void loop() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::AFTER_WIFI; }

  void add_target(const char *name, const char *address, uint16_t port, api::enums::TcpProxyTargetType type,
                  const char *path);
  void set_max_connections(uint8_t max_connections) { this->max_connections_ = max_connections; }
  void set_buffer_size(uint16_t buffer_size) { this->buffer_size_ = buffer_size; }
  void set_connect_timeout(uint32_t timeout_ms) { this->connect_timeout_ = timeout_ms; }

  const std::array<TCPProxyTarget, TCP_PROXY_TARGET_COUNT> &get_targets() const { return this->targets_; }

  // Called by the API layer, from the main loop
  void on_open_request(api::APIConnection *conn, const api::TcpProxyOpenRequest &msg);
  void on_data(api::APIConnection *conn, const api::TcpProxyData &msg);
  void on_window_update(api::APIConnection *conn, const api::TcpProxyWindowUpdate &msg);
  void on_close(api::APIConnection *conn, const api::TcpProxyClose &msg);
  /// The API connection is going away: drop its streams without notifying it
  void on_connection_closed(api::APIConnection *conn);

 protected:
  TCPProxyStream *find_stream_(api::APIConnection *conn, uint32_t stream_id);
  void reject_(api::APIConnection *conn, uint32_t stream_id, api::enums::TcpProxyStatus status);
  void check_connect_(TCPProxyStream &stream);
  void flush_rx_(TCPProxyStream &stream);
  void maybe_send_window_update_(TCPProxyStream &stream);
  void pump_tx_(TCPProxyStream &stream);
  /// Close the socket and queue a close message for the client
  void begin_close_(TCPProxyStream &stream, api::enums::TcpProxyStatus status);
  /// Return the slot to the pool without telling the client anything
  void release_(TCPProxyStream &stream);
  bool send_open_response_(TCPProxyStream &stream, api::enums::TcpProxyStatus status);

  std::array<TCPProxyTarget, TCP_PROXY_TARGET_COUNT> targets_{};
  std::unique_ptr<TCPProxyStream[]> streams_;
  /// Scratch buffer for socket reads, allocated while any stream is active
  std::unique_ptr<uint8_t[]> read_buf_;
  HighFrequencyLoopRequester high_freq_;
  uint32_t connect_timeout_{5000};
  uint16_t buffer_size_{2048};
  uint8_t target_count_{0};
  uint8_t max_connections_{4};
  uint8_t active_{0};
};

extern TCPProxy *global_tcp_proxy;  // NOLINT(cppcoreguidelines-avoid-non-const-global-variables)

}  // namespace esphome::tcp_proxy

#endif  // USE_TCP_PROXY
