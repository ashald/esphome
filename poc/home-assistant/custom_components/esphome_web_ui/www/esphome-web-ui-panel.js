// Panel for ESPHome device web UIs (proof of concept). No build step: plain web component.
//
// /esphome-web-ui                      -> list of device UIs
// /esphome-web-ui/<device_id>[/<n>]    -> UI n (default 0) of a device, in an iframe
//
// The iframe loads /api/esphome_web_ui/<session token>/..., proxied by Home Assistant
// over the device's native API connection. In isolated mode (default) the page runs
// with an opaque origin so it cannot touch Home Assistant's own storage.

const SANDBOX =
  "allow-scripts allow-forms allow-popups allow-popups-to-escape-sandbox allow-modals allow-downloads";
const KEEPALIVE_MS = 60_000;

const STYLE = `
  /* Panels get no definite height from their container, so size to the viewport */
  :host { display: flex; flex-direction: column; height: 100vh; height: 100dvh;
          background: var(--primary-background-color); color: var(--primary-text-color); }
  .toolbar { display: flex; align-items: center; gap: 4px; height: var(--header-height, 56px);
             padding: 0 8px; box-sizing: border-box; flex: none;
             background: var(--app-header-background-color); color: var(--app-header-text-color, white);
             border-bottom: var(--app-header-border-bottom, none); }
  .title { flex: 1; font-size: 20px; margin-left: 8px; white-space: nowrap;
           overflow: hidden; text-overflow: ellipsis; }
  .title small { opacity: .7; font-size: 14px; margin-left: 6px; }
  button, a.button { display: inline-flex; align-items: center; justify-content: center;
           gap: 6px; height: 40px; min-width: 40px; padding: 0 8px; border: 0; border-radius: 20px;
           background: none; color: inherit; font: inherit; font-size: 14px; cursor: pointer;
           text-decoration: none; }
  button:hover, a.button:hover { background: rgba(255,255,255,.12); }
  .menu { display: none; } :host([narrow]) .menu { display: inline-flex; }
  :host([narrow]) .isolation span { display: none; }
  iframe { flex: 1 1 auto; min-height: 0; width: 100%; border: 0; background: white; }
  .content { padding: 16px; max-width: 720px; margin: 0 auto; width: 100%; box-sizing: border-box; }
  .card { display: flex; align-items: center; gap: 16px; padding: 16px; margin-bottom: 8px;
          border-radius: var(--ha-card-border-radius, 12px); background: var(--card-background-color);
          box-shadow: var(--ha-card-box-shadow, none); color: inherit; text-decoration: none;
          border: 1px solid var(--divider-color); }
  .card:hover { border-color: var(--primary-color); }
  .card ha-icon { color: var(--primary-color); }
  .card .name { font-weight: 500; } .card .sub { color: var(--secondary-text-color); font-size: 14px; }
  .message { padding: 32px 16px; text-align: center; color: var(--secondary-text-color); }
`;

function icon(name, fallback) {
  return customElements.get("ha-icon") ? `<ha-icon icon="${name}"></ha-icon>` : fallback;
}

function escape(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

class EsphomeWebUiPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._view = null; // "list:" or "ui:<device>/<target>"
    this._session = null;
    this._timer = null;
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._update();
  }

  set narrow(narrow) {
    this.toggleAttribute("narrow", narrow);
  }

  set route(route) {
    this._route = route;
    if (this._hass) this._update();
  }

  disconnectedCallback() {
    clearInterval(this._timer);
    this._timer = null;
    this._view = null;
  }

  connectedCallback() {
    if (this._hass) this._update();
  }

  _target() {
    const parts = (this._route?.path || "").split("/").filter(Boolean);
    if (!parts.length) return null;
    return { deviceId: parts[0], target: Number(parts[1] || 0) };
  }

  _isolated(deviceId) {
    try {
      return localStorage.getItem(`esphome-web-ui:trusted:${deviceId}`) !== "1";
    } catch {
      return true;
    }
  }

  _update() {
    const target = this._target();
    const view = target ? `ui:${target.deviceId}/${target.target}` : "list:";
    if (view === this._view) return;
    this._view = view;
    clearInterval(this._timer);
    this._timer = null;
    this._session = null;
    if (target) this._showUi(target);
    else this._showList();
  }

  _toolbar(title, subtitle, actions = "") {
    return `
      <style>${STYLE}</style>
      <div class="toolbar">
        <button class="menu" title="Menu">${icon("mdi:menu", "☰")}</button>
        <div class="title">${escape(title)}${subtitle ? `<small>${escape(subtitle)}</small>` : ""}</div>
        ${actions}
      </div>`;
  }

  _bindMenu() {
    this.shadowRoot.querySelector(".menu").addEventListener("click", () =>
      this.dispatchEvent(new Event("hass-toggle-menu", { bubbles: true, composed: true })),
    );
  }

  _navigate(path) {
    history.pushState(null, "", path);
    window.dispatchEvent(new CustomEvent("location-changed"));
  }

  async _showList() {
    this.shadowRoot.innerHTML =
      this._toolbar("Device UIs") + `<div class="content"><div class="message">Loading…</div></div>`;
    this._bindMenu();
    let uis;
    try {
      uis = await this._hass.callWS({ type: "esphome_web_ui/list" });
    } catch (err) {
      uis = null;
      this.shadowRoot.querySelector(".content").innerHTML =
        `<div class="message">${escape(err.message || String(err))}</div>`;
    }
    if (!uis || this._view !== "list:") return;
    const content = this.shadowRoot.querySelector(".content");
    if (!uis.length) {
      content.innerHTML = `<div class="message">No connected ESPHome device offers a web UI.<br>
        Add <code>tcp_proxy:</code> to a device's configuration.</div>`;
      return;
    }
    content.innerHTML = uis
      .map(
        (ui) => `
        <a class="card" href="/esphome-web-ui/${ui.device_id}/${ui.target}">
          ${icon("mdi:monitor-dashboard", "")}
          <div><div class="name">${escape(ui.device_name)}</div>
               <div class="sub">${escape(ui.name)}</div></div>
        </a>`,
      )
      .join("");
    content.querySelectorAll("a.card").forEach((a) =>
      a.addEventListener("click", (ev) => {
        ev.preventDefault();
        this._navigate(a.getAttribute("href"));
      }),
    );
  }

  async _openSession({ deviceId, target }, token) {
    return this._hass.callWS({
      type: "esphome_web_ui/session",
      device_id: deviceId,
      target,
      isolated: this._isolated(deviceId),
      ...(token ? { token } : {}),
    });
  }

  async _showUi(target) {
    const view = this._view;
    this.shadowRoot.innerHTML = this._toolbar("Device UI") + `<div class="message">Connecting…</div>`;
    this._bindMenu();
    let session;
    try {
      session = await this._openSession(target);
    } catch (err) {
      if (this._view !== view) return;
      this.shadowRoot.querySelector(".message").textContent = err.message || String(err);
      return;
    }
    if (this._view !== view) return;
    this._session = session;
    const isolated = this._isolated(target.deviceId);
    this.shadowRoot.innerHTML =
      this._toolbar(
        session.device_name,
        session.name,
        `<button class="back" title="All device UIs">${icon("mdi:view-list", "≡")}</button>
         <button class="isolation" title="${
           isolated
             ? "Isolated: the page cannot access Home Assistant. Click to trust this device."
             : "Trusted: the page runs with Home Assistant's origin. Click to isolate."
         }">${icon(isolated ? "mdi:shield-lock" : "mdi:shield-off-outline", isolated ? "🔒" : "🔓")}
           <span>${isolated ? "Isolated" : "Trusted"}</span></button>
         <button class="reload" title="Reload">${icon("mdi:refresh", "⟳")}</button>
         <a class="button" title="Open in a new tab" target="_blank" rel="noopener"
            href="${session.url}">${icon("mdi:open-in-new", "↗")}</a>`,
      ) +
      `<iframe title="${escape(session.name)}" src="${session.url}"
               ${isolated ? `sandbox="${SANDBOX}"` : ""} allow="fullscreen"></iframe>`;
    this._bindMenu();
    const root = this.shadowRoot;
    root.querySelector(".back").addEventListener("click", () => this._navigate("/esphome-web-ui"));
    root.querySelector(".reload").addEventListener("click", () => {
      root.querySelector("iframe").src = this._session.url;
    });
    root.querySelector(".isolation").addEventListener("click", () => {
      try {
        const key = `esphome-web-ui:trusted:${target.deviceId}`;
        if (isolated) localStorage.setItem(key, "1");
        else localStorage.removeItem(key);
      } catch {
        /* storage unavailable: stays isolated */
      }
      this._view = null;
      this._update();
    });
    // Keep the session alive while the panel is open, even if the UI goes quiet
    this._timer = setInterval(async () => {
      try {
        const next = await this._openSession(target, this._session.token);
        if (next.token !== this._session.token) {
          this._session = next;
          root.querySelector("iframe").src = next.url;
        }
      } catch {
        /* device offline; the iframe shows the proxy's error on next request */
      }
    }, KEEPALIVE_MS);
  }
}

customElements.define("esphome-web-ui-panel", EsphomeWebUiPanel);
