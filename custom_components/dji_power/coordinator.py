"""DataUpdateCoordinator + MQTT handler for DJI Power Station."""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from datetime import timedelta
from typing import Any

import paho.mqtt.client as mqtt
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import DJIPowerAPI, DJIAuthError, DJIAPIError
from .const import (
    DOMAIN,
    MQTT_HOST,
    MQTT_PORT,
    MQTT_KEEPALIVE,
    MQTT_TOPIC_PROPERTY,
    MQTT_TOPIC_EVENTS,
    POLL_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

# Seconds before MQTT token expiry at which we refresh
MQTT_TOKEN_REFRESH_MARGIN = 300  # 5 minutes

# If no MQTT message arrives within this many seconds, restart the connection
MQTT_WATCHDOG_TIMEOUT = 600   # 10 minutes
MQTT_WATCHDOG_INTERVAL = 120  # check every 2 minutes


class DJIPowerCoordinator(DataUpdateCoordinator):
    """Manages REST polling + MQTT live updates for one DJI power device."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: DJIPowerAPI,
        sn: str,
        device_name: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{sn}",
            update_interval=timedelta(seconds=POLL_INTERVAL),
        )
        self.api = api
        self.sn = sn
        self.device_name = device_name

        # Live state — updated by MQTT or REST poll
        self.state: dict[str, Any] = {}

        # Energy accumulators (kWh) — seeded from HA recorder on startup
        # via DJIPowerEnergySensor.async_added_to_hass, then grown here.
        self._last_energy_ts: float = 0.0

        # MQTT internals
        self._mqtt_client: mqtt.Client | None = None
        self._mqtt_thread: threading.Thread | None = None
        self._mqtt_token: str | None = None
        self._mqtt_user_uuid: str | None = None
        self._mqtt_token_expires_at: float = 0
        self._mqtt_running = False

        # Watchdog: cancel function returned by async_track_time_interval
        self._mqtt_watchdog_cancel: Any = None

    # ------------------------------------------------------------------
    # REST poll (fallback / initial data)
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from REST API (runs every POLL_INTERVAL seconds)."""
        try:
            devices = await self.api.get_devices()
        except DJIAuthError as exc:
            # Raise ConfigEntryAuthFailed so HA shows the re-auth notification
            # and stops hammering the API with bad credentials.
            raise ConfigEntryAuthFailed(
                f"DJI token expired — please re-enter your x-member-token: {exc}"
            ) from exc
        except DJIAPIError as exc:
            raise UpdateFailed(f"API error: {exc}") from exc

        for dev in devices:
            if dev.get("base_info", {}).get("sn") == self.sn:
                base = dev["base_info"]
                self.state.setdefault("sn", self.sn)
                self.state["name"] = base.get("name", self.device_name)
                self.state["soc"] = base.get("battery", 0) / 100  # centipercent → %
                self.state["online"] = base.get("online_status", False)
                self.state["device_mode"] = base.get("device_mode")

                if "_last_mqtt" in self.state:
                    power_in = self.state.get("power_in", 0) or 0
                    self.state["is_charging"] = power_in > 5
                else:
                    self.state["is_charging"] = bool(base.get("is_charging", False))
                    self.state.setdefault("charge_type", 0)

                self.state.setdefault("power_in", 0)
                self.state.setdefault("power_out", 0)
                self.state.setdefault("temperature", None)
                self.state.setdefault("remain_time", None)
                return self.state

        raise UpdateFailed(f"Device {self.sn} not found in device list")

    # ------------------------------------------------------------------
    # MQTT setup / teardown
    # ------------------------------------------------------------------

    async def async_start_mqtt(self) -> None:
        """Start MQTT connection and watchdog (called from async context)."""
        self._mqtt_running = True
        await self._async_connect_mqtt()

        # Start watchdog — checks every 2 min if MQTT is still delivering data
        self._mqtt_watchdog_cancel = async_track_time_interval(
            self.hass,
            self._async_mqtt_watchdog,
            timedelta(seconds=MQTT_WATCHDOG_INTERVAL),
        )

    async def _async_connect_mqtt(self) -> None:
        """Fetch fresh credentials and (re)start the MQTT client."""
        try:
            creds = await self.api.get_mqtt_credentials()
        except DJIAuthError as exc:
            _LOGGER.error(
                "Cannot get MQTT credentials for %s — member token expired: %s. "
                "Re-authenticate in HA Settings → Integrations.",
                self.sn, exc,
            )
            return
        except Exception as exc:
            _LOGGER.warning("Cannot get MQTT credentials for %s: %s", self.sn, exc)
            return

        self._mqtt_token = creds["user_token"]
        self._mqtt_user_uuid = creds["user_uuid"]
        expire = creds.get("expire", 3600)
        self._mqtt_token_expires_at = time.time() + expire - MQTT_TOKEN_REFRESH_MARGIN

        _LOGGER.debug(
            "MQTT credentials obtained for %s (expires in %ds)", self.sn, expire
        )

        self._start_mqtt_client(creds["client_id"], self._mqtt_user_uuid, self._mqtt_token)

        # Schedule proactive token refresh before expiry
        self._schedule_mqtt_token_refresh(expire - MQTT_TOKEN_REFRESH_MARGIN)

    def _start_mqtt_client(self, client_id: str, user_uuid: str, token: str) -> None:
        """Tear down any existing paho client and start a fresh one."""
        # Cleanly stop the old client (disconnect causes loop_forever to exit)
        if self._mqtt_client is not None:
            try:
                self._mqtt_client.disconnect()
            except Exception:
                pass
            self._mqtt_client = None

        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            protocol=mqtt.MQTTv5,
        )
        client.username_pw_set(username=user_uuid, password=token)
        client.tls_set()
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect
        self._mqtt_client = client

        thread = threading.Thread(
            target=self._mqtt_loop,
            args=(client,),
            daemon=True,
            name=f"dji_mqtt_{self.sn}",
        )
        self._mqtt_thread = thread
        thread.start()

    def _schedule_mqtt_token_refresh(self, delay_seconds: float) -> None:
        """Schedule _async_refresh_mqtt_token after delay_seconds.

        Uses call_later + async_create_task — the correct pattern for
        scheduling a coroutine from within the HA event loop.
        """
        def _fire():
            self.hass.async_create_task(
                self._async_refresh_mqtt_token(),
                name=f"dji_mqtt_refresh_{self.sn}",
            )

        self.hass.loop.call_later(max(delay_seconds, 60), _fire)

    def _mqtt_loop(self, client: mqtt.Client) -> None:
        """Run a single paho client in a dedicated background thread.

        Receives the client as an argument so that if _start_mqtt_client
        creates a replacement, this thread uses its own reference and exits
        cleanly when disconnect() is called on it.
        """
        try:
            client.connect(MQTT_HOST, MQTT_PORT, keepalive=MQTT_KEEPALIVE)
            client.loop_forever(retry_first_connection=True)
        except Exception as exc:
            _LOGGER.warning("MQTT loop error for %s: %s", self.sn, exc)

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if str(reason_code) == "Success":
            topic_prop = MQTT_TOPIC_PROPERTY.format(sn=self.sn)
            topic_events = MQTT_TOPIC_EVENTS.format(sn=self.sn)
            client.subscribe(topic_prop, qos=0)
            client.subscribe(topic_events, qos=0)
            _LOGGER.info("MQTT connected for %s, subscribed to %s", self.sn, topic_prop)
        else:
            _LOGGER.warning("MQTT connect failed for %s: %s", self.sn, reason_code)

    def _on_message(self, client, userdata, msg) -> None:
        try:
            payload = json.loads(msg.payload.decode())
            method = payload.get("method", "")

            if method == "device_osd":
                host = payload.get("data", {}).get("host", {})
                update = self._parse_osd_to_dict(host)
                asyncio.run_coroutine_threadsafe(
                    self._async_merge_and_notify(update), self.hass.loop
                )
        except Exception as exc:
            _LOGGER.debug("Error parsing MQTT message: %s", exc)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties) -> None:
        if self._mqtt_running and client is self._mqtt_client:
            _LOGGER.warning(
                "MQTT disconnected for %s (rc=%s) — paho will retry automatically",
                self.sn, reason_code,
            )

    # ------------------------------------------------------------------
    # Watchdog — restarts MQTT if no messages arrive for too long
    # ------------------------------------------------------------------

    async def _async_mqtt_watchdog(self, now=None) -> None:
        """Called every MQTT_WATCHDOG_INTERVAL seconds by async_track_time_interval.

        If the device is online but we haven't received an MQTT message in
        MQTT_WATCHDOG_TIMEOUT seconds, the connection has silently died.
        Fetch fresh credentials and restart the client.
        """
        if not self._mqtt_running:
            return

        last_mqtt = self.state.get("_last_mqtt", 0)
        age = time.time() - last_mqtt

        # Only trigger if we had at least one message before (last_mqtt > 0)
        # and the gap is larger than the timeout.
        if last_mqtt > 0 and age > MQTT_WATCHDOG_TIMEOUT:
            _LOGGER.warning(
                "MQTT watchdog: no message from %s for %.0f s — restarting connection",
                self.sn, age,
            )
            await self._async_connect_mqtt()

    # ------------------------------------------------------------------
    # Proactive MQTT token refresh
    # ------------------------------------------------------------------

    async def _async_refresh_mqtt_token(self) -> None:
        """Refresh the MQTT token before it expires."""
        if not self._mqtt_running:
            return

        _LOGGER.debug("Proactively refreshing MQTT token for %s", self.sn)
        # _async_connect_mqtt fetches new creds, restarts the client, and
        # re-schedules the next refresh — all in one place.
        await self._async_connect_mqtt()

    # ------------------------------------------------------------------
    # OSD parsing + state merge
    # ------------------------------------------------------------------

    def _parse_osd_to_dict(self, host: dict) -> dict:
        """Parse device_osd host block; returns a partial state dict (thread-safe, no mutation)."""
        update: dict = {"online": True, "_last_mqtt": time.time()}
        battery = host.get("battery", {})
        power_info = host.get("power_info", {})

        if battery:
            charge_pct = battery.get("charge_pct")
            if charge_pct is not None:
                update["soc"] = charge_pct / 100  # centipercent → %

            remain = battery.get("remain_time")
            if remain is not None:
                update["remain_time"] = remain  # seconds

            temp = battery.get("temp")
            if temp is not None:
                update["temperature"] = temp / 100  # centi-°C → °C

            charge_type = battery.get("charge_type")
            if charge_type is not None:
                update["charge_type"] = charge_type

        # AC output state: sw=0 = ON, sw=1 = OFF
        interfaces = host.get("power_info", {}).get("interfaces", [])
        for iface in interfaces:
            if iface.get("group_type") == 2:
                for item in iface.get("list", []):
                    sw = item.get("sw")
                    if sw is not None:
                        update["ac_output_enabled"] = (sw == 0)
                break

        if power_info:
            update["power_in"] = power_info.get("input", 0)
            update["power_out"] = power_info.get("output", 0)

        # Derive is_charging from power_in — charge_type unreliable on this firmware
        update["is_charging"] = (update.get("power_in", 0) or 0) > 5

        return update

    def _integrate_energy(self, power_in_w: float, power_out_w: float) -> None:
        """Accumulate energy totals (kWh) from instantaneous power readings (W)."""
        now = time.time()
        if self._last_energy_ts > 0:
            dt_h = (now - self._last_energy_ts) / 3600.0
            if 0 < dt_h <= 5 / 60:  # ignore gaps > 5 min
                self.state["energy_in"] = round(
                    self.state.get("energy_in", 0.0) + power_in_w * dt_h / 1000,
                    6,
                )
                self.state["energy_out"] = round(
                    self.state.get("energy_out", 0.0) + power_out_w * dt_h / 1000,
                    6,
                )
        self._last_energy_ts = now

    async def _async_merge_and_notify(self, update: dict) -> None:
        """Merge MQTT update into state on the HA event loop and notify listeners."""
        power_in = float(update.get("power_in", self.state.get("power_in", 0)) or 0)
        power_out = float(update.get("power_out", self.state.get("power_out", 0)) or 0)
        self._integrate_energy(power_in, power_out)

        merged = {**self.state, **update}
        merged["energy_in"] = self.state.get("energy_in", 0.0)
        merged["energy_out"] = self.state.get("energy_out", 0.0)
        self.state = merged
        self.async_set_updated_data(merged)

    # ------------------------------------------------------------------
    # AC output command (best-effort)
    # ------------------------------------------------------------------

    def publish_ac_output(self, enabled: bool) -> None:
        """Publish AC output command via MQTT (sw=0=ON, sw=1=OFF).

        Best-effort: broker ACL currently blocks user-client publishes to
        the forward/ topic, so the device may not respond.
        """
        if not self._mqtt_client:
            return
        import uuid as _uuid
        sw = 0 if enabled else 1
        topic = f"forward/dy/thing/product/{self.sn}/services"
        payload = json.dumps({
            "tid": str(_uuid.uuid4()),
            "bid": str(_uuid.uuid4()),
            "timestamp": int(time.time() * 1000),
            "app_id": "dy301",
            "method": "output_switch",
            "data": {"group_type": 2, "sw": sw},
        })
        self._mqtt_client.publish(topic, payload, qos=1)
        _LOGGER.debug("Published AC output command (sw=%d) to %s", sw, topic)

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    async def async_stop_mqtt(self) -> None:
        """Stop MQTT client and watchdog."""
        self._mqtt_running = False

        if self._mqtt_watchdog_cancel is not None:
            self._mqtt_watchdog_cancel()
            self._mqtt_watchdog_cancel = None

        if self._mqtt_client is not None:
            try:
                self._mqtt_client.disconnect()
            except Exception:
                pass
            self._mqtt_client = None
