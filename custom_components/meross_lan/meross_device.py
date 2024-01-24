from __future__ import annotations

import abc
import asyncio
import bisect
from datetime import datetime, timezone, tzinfo
from json import JSONDecodeError
from time import time
import typing
import weakref
from zoneinfo import ZoneInfo

from aiohttp import ServerDisconnectedError
from homeassistant.core import callback
from homeassistant.helpers import device_registry
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util
import voluptuous as vol

from . import const as mlc
from .const import (
    CONF_DEVICE_ID,
    CONF_HOST,
    CONF_PAYLOAD,
    CONF_POLLING_PERIOD,
    CONF_POLLING_PERIOD_DEFAULT,
    CONF_POLLING_PERIOD_MIN,
    CONF_PROTOCOL,
    CONF_PROTOCOL_AUTO,
    CONF_PROTOCOL_HTTP,
    CONF_PROTOCOL_MQTT,
    CONF_PROTOCOL_OPTIONS,
    CONF_TIMESTAMP,
    DOMAIN,
    PARAM_HEADER_SIZE,
    PARAM_HEARTBEAT_PERIOD,
    PARAM_INFINITE_EPOCH,
    PARAM_TIMESTAMP_TOLERANCE,
    PARAM_TIMEZONE_CHECK_NOTOK_PERIOD,
    PARAM_TIMEZONE_CHECK_OK_PERIOD,
    PARAM_TRACING_ABILITY_POLL_TIMEOUT,
    DeviceConfigType,
)
from .helpers import datetime_from_epoch, schedule_async_callback, schedule_callback
from .helpers.manager import ApiProfile, ConfigEntryManager, EntityManager
from .helpers.namespaces import EntityPollingStrategy, NamespaceHandler, PollingStrategy
from .meross_entity import MerossFakeEntity
from .merossclient import (
    HostAddress,
    MerossRequest,
    MerossResponse,
    const as mc,
    get_default_arguments,
    get_message_signature,
    get_message_uuid,
    get_port_safe,
    is_device_online,
    json_dumps,
)
from .merossclient.httpclient import MerossHttpClient, TerminatedException
from .repairs import IssueSeverity, create_issue, remove_issue
from .sensor import PERCENTAGE, MLSensor, ProtocolSensor
from .update import MLUpdate

if typing.TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .meross_entity import MerossEntity
    from .meross_profile import MQTTConnection
    from .merossclient import (
        MerossDeviceDescriptor,
        MerossHeaderType,
        MerossMessage,
        MerossMessageType,
        MerossPayloadType,
        MerossRequestType,
    )
    from .merossclient.cloudapi import (
        DeviceInfoType,
        LatestVersionType,
        SubDeviceInfoType,
    )

# when tracing we enumerate appliance abilities to get insights on payload structures
# this list will be excluded from enumeration since it's redundant/exposing sensitive info
# or simply crashes/hangs the device
TRACE_ABILITY_EXCLUDE = (
    mc.NS_APPLIANCE_SYSTEM_ALL,
    mc.NS_APPLIANCE_SYSTEM_ABILITY,
    mc.NS_APPLIANCE_SYSTEM_DNDMODE,
    mc.NS_APPLIANCE_SYSTEM_TIME,
    mc.NS_APPLIANCE_SYSTEM_HARDWARE,
    mc.NS_APPLIANCE_SYSTEM_FIRMWARE,
    mc.NS_APPLIANCE_SYSTEM_ONLINE,
    mc.NS_APPLIANCE_SYSTEM_REPORT,
    mc.NS_APPLIANCE_SYSTEM_CLOCK,
    mc.NS_APPLIANCE_SYSTEM_POSITION,
    mc.NS_APPLIANCE_DIGEST_TRIGGERX,
    mc.NS_APPLIANCE_DIGEST_TIMERX,
    mc.NS_APPLIANCE_CONFIG_KEY,
    mc.NS_APPLIANCE_CONFIG_WIFI,
    mc.NS_APPLIANCE_CONFIG_WIFIX,  # disconnects
    mc.NS_APPLIANCE_CONFIG_WIFILIST,
    mc.NS_APPLIANCE_CONFIG_TRACE,
    mc.NS_APPLIANCE_CONTROL_BIND,
    mc.NS_APPLIANCE_CONTROL_UNBIND,
    mc.NS_APPLIANCE_CONTROL_MULTIPLE,
    mc.NS_APPLIANCE_CONTROL_UPGRADE,  # disconnects
    mc.NS_APPLIANCE_CONTROL_TRIGGERX,
    mc.NS_APPLIANCE_CONTROL_TIMERX,
    mc.NS_APPLIANCE_HUB_EXCEPTION,  # disconnects
    mc.NS_APPLIANCE_HUB_REPORT,  # disconnects
    mc.NS_APPLIANCE_HUB_SUBDEVICELIST,  # disconnects
    mc.NS_APPLIANCE_HUB_PAIRSUBDEV,  # disconnects
    mc.NS_APPLIANCE_MCU_UPGRADE,  # disconnects
    mc.NS_APPLIANCE_MCU_HP110_PREVIEW,  # disconnects
    mc.NS_APPLIANCE_MCU_FIRMWARE,  # disconnects
    mc.NS_APPLIANCE_CONTROL_PHYSICALLOCK,  # disconnects
)

TIMEZONES_SET = None


class MerossDeviceBase(EntityManager):
    """
    Abstract base class for MerossDevice and MerossSubDevice (from hub)
    giving common behaviors like device_registry interface
    """

    deviceentry_id: dict[str, set[tuple[str, str]]]
    # device info dict from meross cloud api
    device_info: DeviceInfoType | SubDeviceInfoType | None

    __slots__ = (
        "device_info",
        "_online",
        "_device_registry_entry",
    )

    def __init__(
        self,
        id: str,
        *,
        config_entry_id: str,
        default_name: str,
        model: str,
        hw_version: str | None = None,
        sw_version: str | None = None,
        connections: set[tuple[str, str]] | None = None,
        via_device: tuple[str, str] | None = None,
        **kwargs,
    ):
        super().__init__(
            id,
            config_entry_id=config_entry_id,
            deviceentry_id={"identifiers": {(DOMAIN, id)}},
            **kwargs,
        )
        self.device_info = None
        self._online = False
        self._device_registry_entry = None
        with self.exception_warning("DeviceRegistry.async_get_or_create"):
            self._device_registry_entry = weakref.ref(
                device_registry.async_get(self.hass).async_get_or_create(
                    config_entry_id=self.config_entry_id,
                    connections=connections,
                    manufacturer=mc.MANUFACTURER,
                    name=default_name,
                    model=model,
                    hw_version=hw_version,
                    sw_version=sw_version,
                    via_device=via_device,
                    **self.deviceentry_id,  # type: ignore
                )
            )

    # interface: EntityManager
    @property
    def name(self) -> str:
        """
        returns a proper (friendly) device name for logging purposes
        """
        if _device_registry_entry := self.device_registry_entry:
            return (
                _device_registry_entry.name_by_user
                or _device_registry_entry.name
                or self._get_internal_name()
            )
        return self._get_internal_name()

    @property
    def online(self):
        return self._online

    async def async_shutdown(self):
        await super().async_shutdown()
        self._device_registry_entry = None
        self.device_info = None

    # interface: self
    @property
    def device_registry_entry(self):
        _device_registry_entry = (
            self._device_registry_entry and self._device_registry_entry()
        )
        if _device_registry_entry is None:
            _device_registry_entry = device_registry.async_get(
                self.hass
            ).async_get_device(**self.deviceentry_id)
            if _device_registry_entry:
                self._device_registry_entry = weakref.ref(_device_registry_entry)
        return _device_registry_entry

    def update_device_info(self, device_info: DeviceInfoType | SubDeviceInfoType):
        self.device_info = device_info
        if _device_registry_entry := self.device_registry_entry:
            name = (
                device_info.get(self._get_device_info_name_key())
                or self._get_internal_name()
            )
            if name != _device_registry_entry.name:
                device_registry.async_get(self.hass).async_update_device(
                    _device_registry_entry.id, name=name
                )

    def build_request(
        self,
        namespace: str,
        method: str,
        payload: MerossPayloadType,
    ) -> MerossRequest:
        raise NotImplementedError

    async def async_request_raw(
        self,
        request: MerossRequest,
    ) -> MerossResponse | None:
        raise NotImplementedError

    async def async_request(
        self,
        namespace: str,
        method: str,
        payload: MerossPayloadType,
    ) -> MerossResponse | None:
        raise NotImplementedError

    async def async_request_ack(
        self,
        namespace: str,
        method: str,
        payload: MerossPayloadType,
    ) -> MerossResponse | None:
        response = await self.async_request(namespace, method, payload)
        return (
            response
            if response and response[mc.KEY_HEADER][mc.KEY_METHOD] != mc.METHOD_ERROR
            else None
        )

    def request(self, request_tuple: MerossRequestType):
        return self.hass.async_create_task(self.async_request(*request_tuple))

    @property
    @abc.abstractmethod
    def tz(self) -> tzinfo:
        return None

    def check_device_timezone(self):
        raise NotImplementedError

    @abc.abstractmethod
    def _get_device_info_name_key(self) -> str:
        return ""

    @abc.abstractmethod
    def _get_internal_name(self) -> str:
        return ""

    def _set_online(self):
        self.log(self.DEBUG, "Back online!")
        self._online = True

    def _set_offline(self):
        self.log(self.DEBUG, "Going offline!")
        self._online = False
        for entity in self.entities.values():
            entity.set_unavailable()


class SystemDebugPollingStrategy(PollingStrategy):
    """
    Polling strategy for NS_APPLIANCE_SYSTEM_DEBUG. This
    query, beside carrying some device info, is only useful for us
    in order to see if the device reports it is mqtt-connected
    and allows us to update the MQTT connection state. The whole
    polling strategy is only added at runtime when the device has
    a corresponding cloud profile and conf_protocol is CONF_PROTOCOL_AUTO
    it will then kick in only if we're not (yet) mqtt connected
    but we should
    """

    async def async_poll(
        self, device: MerossDevice, epoch: float, namespace: str | None
    ):
        if not device._mqtt_active:
            await device.async_request_poll(self)


class MerossDevice(ConfigEntryManager, MerossDeviceBase):
    """
    Generic protocol handler class managing the physical device stack/state
    """

    DEFAULT_PLATFORMS = ConfigEntryManager.DEFAULT_PLATFORMS | {
        MLUpdate.PLATFORM: None,
    }

    # these are set from ConfigEntry
    config: DeviceConfigType
    polling_period: int
    _polling_delay: int
    conf_protocol: str
    pref_protocol: str
    curr_protocol: str
    # other default property values
    device_timestamp: int
    _tzinfo: ZoneInfo | None  # smart cache of device tzinfo
    _unsub_polling_callback: asyncio.TimerHandle | None
    entity_dnd: MerossEntity
    sensor_protocol: ProtocolSensor
    sensor_signal_strength: MLSensor
    update_firmware: MLUpdate | None

    __slots__ = (
        "polling_period",
        "_polling_delay",
        "conf_protocol",
        "pref_protocol",
        "curr_protocol",
        "descriptor",
        "needsave",
        "device_timestamp",
        "device_timedelta",
        "device_timedelta_log_epoch",
        "device_timedelta_config_epoch",
        "device_debug",
        "device_response_size_min",
        "device_response_size_max",
        "lastrequest",
        "lastresponse",
        "_topic_response",  # sets the "from" field in request messages
        "_profile",
        "_mqtt_connection",  # we're binded to an MQTT profile/broker
        "_mqtt_connected",  # the broker is online/connected
        "_mqtt_publish",  # the broker accepts 'publish' (cloud broker conf might disable publishing)
        "_mqtt_active",  # the broker receives valid traffic i.e. the device is 'mqtt' reachable
        "_mqtt_lastrequest",
        "_mqtt_lastresponse",
        "_http",  # cached MerossHttpClient
        "_http_active",  # HTTP is 'online' i.e. reachable
        "_http_lastrequest",
        "_http_lastresponse",
        "namespace_handlers",
        "polling_strategies",
        "_unsub_polling_callback",
        "_queued_smartpoll_requests",
        "_multiple_len",
        "_multiple_len_max",
        "_multiple_requests",
        "_multiple_response_size",
        "_tzinfo",
        "_timezone_next_check",
        "_unsub_trace_ability_callback",
        "entity_dnd",
        "sensor_protocol",
        "sensor_signal_strength",
        "update_firmware",
    )

    def __init__(
        self,
        descriptor: MerossDeviceDescriptor,
        config_entry: ConfigEntry,
    ):
        self.descriptor = descriptor
        self.needsave = False
        self.curr_protocol = CONF_PROTOCOL_AUTO
        self.device_timestamp = 0
        self.device_timedelta = 0
        self.device_timedelta_log_epoch = 0
        self.device_timedelta_config_epoch = 0
        self.device_debug = {}
        self.device_response_size_min = 2000
        self.device_response_size_max = 5000
        self.lastrequest = 0.0
        self.lastresponse = 0.0
        self._topic_response = mc.MANUFACTURER
        self._profile: ApiProfile | None = None
        self._mqtt_connection: MQTTConnection | None = None
        self._mqtt_connected: MQTTConnection | None = None
        self._mqtt_publish: MQTTConnection | None = None
        self._mqtt_active: MQTTConnection | None = None
        self._mqtt_lastrequest = 0
        self._mqtt_lastresponse = 0
        self._http: MerossHttpClient | None = None
        self._http_active: MerossHttpClient | None = None
        self._http_lastrequest = 0
        self._http_lastresponse = 0
        self.namespace_handlers: dict[str, NamespaceHandler] = {}
        self.polling_strategies: dict[str, PollingStrategy] = {}
        # TODO: try to cache the system.all payload size in order to avoid the json_dumps
        PollingStrategy(self, mc.NS_APPLIANCE_SYSTEM_ALL).response_size = (
            len(json_dumps(descriptor.all)) + PARAM_HEADER_SIZE
        )
        self._unsub_polling_callback = None
        self._queued_smartpoll_requests = 0
        ability: typing.Final = descriptor.ability
        self._multiple_len = ability.get(mc.NS_APPLIANCE_CONTROL_MULTIPLE, {}).get(
            "maxCmdNum", 0
        )
        self._multiple_len_max: typing.Final = self._multiple_len
        self._multiple_requests: list[MerossRequestType] = []
        self._multiple_response_size = PARAM_HEADER_SIZE
        # Message handling is actually very hybrid:
        # when a message (device reply or originated) is received it gets routed to the
        # device instance in 'receive'. Here, it was traditionally parsed with a
        # switch structure against the different expected namespaces.
        # Now the architecture, while still in place, is being moved to handler methods
        # which are looked up by inspecting self for a proper '_handler_{namespace}' signature
        # This signature could be added at runtime or (better I guess) could be added by
        # dedicated mixin classes used to build the actual device class when the device is setup
        # (see __init__.MerossApi.build_device)
        # The handlers dictionary is anyway parsed first and could override a build-time handler.
        # The dicionary keys are Meross namespaces matched against when the message enters the handling
        # self.handlers: Dict[str, Callable] = {} actually disabled!

        self._tzinfo = None
        self._timezone_next_check = (
            0 if mc.NS_APPLIANCE_SYSTEM_TIME in ability else PARAM_INFINITE_EPOCH
        )
        """Indicates the (next) time we should perform a check (only when localmqtt)
        in order to see if the device has correct timezone/dst configuration"""
        self._unsub_trace_ability_callback = None

        # base init after setting some key properties needed for logging
        super().__init__(
            config_entry.data[CONF_DEVICE_ID],
            config_entry,
            default_name=descriptor.productname,
            model=descriptor.productmodel,
            hw_version=descriptor.hardwareVersion,
            sw_version=descriptor.firmwareVersion,
            connections={
                (device_registry.CONNECTION_NETWORK_MAC, descriptor.macAddress)
            },
        )

        self._update_config()

        if mc.NS_APPLIANCE_SYSTEM_DNDMODE in ability:
            from .light import MLDNDLightEntity

            self.entity_dnd = MLDNDLightEntity(self)
            EntityPollingStrategy(self, mc.NS_APPLIANCE_SYSTEM_DNDMODE, self.entity_dnd)
        else:
            self.entity_dnd = MerossFakeEntity  # type: ignore

        self.sensor_protocol = ProtocolSensor(self)

        if mc.NS_APPLIANCE_SYSTEM_RUNTIME in ability:
            self.sensor_signal_strength = sensor_signal_strength = MLSensor(
                self, None, "signal_strength", None
            )
            sensor_signal_strength._attr_entity_category = (
                MLSensor.EntityCategory.DIAGNOSTIC
            )
            sensor_signal_strength._attr_native_unit_of_measurement = PERCENTAGE
            sensor_signal_strength._attr_icon = "mdi:wifi"
            EntityPollingStrategy(
                self, mc.NS_APPLIANCE_SYSTEM_RUNTIME, sensor_signal_strength
            )
        else:
            self.sensor_signal_strength = MerossFakeEntity  # type: ignore

        # the update entity will only be instantiated 'on demand' since
        # we might not have this for devices not related to a cloud profile
        self.update_firmware = None

        for _key, _digest in descriptor.digest.items():
            # _init_xxxx methods provided by mixins
            _init_method_name = f"_init_{_key}"
            if _init := getattr(self, _init_method_name, None):
                with self.exception_warning(_init_method_name):
                    _init(_digest)

    # interface: ConfigEntryManager
    @callback
    async def entry_update_listener(
        self, hass: HomeAssistant, config_entry: ConfigEntry
    ):
        await super().entry_update_listener(hass, config_entry)
        self._update_config()
        self._check_mqtt_connection_attach()

        if self.conf_protocol is not CONF_PROTOCOL_AUTO:
            if self.curr_protocol is not self.conf_protocol:
                self._switch_protocol(self.conf_protocol)

        if http := self._http:
            if self.conf_protocol is CONF_PROTOCOL_MQTT:
                self._http = self._http_active = None
                self.sensor_protocol.update_attr_inactive(ProtocolSensor.ATTR_HTTP)
            else:
                if host := self.host:
                    http.host = host

        # config_entry update might come from DHCP or OptionsFlowHandler address update
        # so we'll eventually retry querying the device
        if not self._online:
            self.request(get_default_arguments(mc.NS_APPLIANCE_SYSTEM_ALL))

    def get_logger_name(self) -> str:
        return f"{self.descriptor.type}_{self.loggable_device_id(self.id)}"

    def _trace_opened(self, epoch: float):
        descr = self.descriptor
        # at this stage, _trace_ability will just (re)schedule itself so
        # the first data in trace will anyway be the ns_all and ns_ability
        # calling it first is needed so that any failure writing leading to
        # a trace_closed will rightly cancel the callback
        self._trace_ability(iter(descr.ability))
        self.trace(epoch, descr.all, mc.NS_APPLIANCE_SYSTEM_ALL)
        self.trace(epoch, descr.ability, mc.NS_APPLIANCE_SYSTEM_ABILITY)

    def _trace_closed(self):
        if self._unsub_trace_ability_callback:
            self._unsub_trace_ability_callback.cancel()
            self._unsub_trace_ability_callback = None

    # interface: MerossDeviceBase
    async def async_shutdown(self):
        remove_issue(mlc.ISSUE_DEVICE_TIMEZONE, self.id)
        if self._mqtt_connection:
            self._mqtt_connection.detach(self)
        if self._profile:
            self._profile.unlink(self)
        if self._http:
            await self._http.async_terminate()
        while self._unsub_polling_callback is None:
            # wait for the polling loop to finish in case
            await asyncio.sleep(1)
        self._unsub_polling_callback.cancel()
        self._unsub_polling_callback = None
        self.polling_strategies.clear()
        self.namespace_handlers.clear()
        await super().async_shutdown()
        ApiProfile.devices[self.id] = None
        self.entity_dnd = None  # type: ignore
        self.sensor_signal_strength = None  # type: ignore
        self.sensor_protocol = None  # type: ignore
        self.update_firmware = None

    def build_request(
        self,
        namespace: str,
        method: str,
        payload: MerossPayloadType,
    ) -> MerossRequest:
        return MerossRequest(self.key, namespace, method, payload, self._topic_response)

    async def async_request_raw(
        self,
        request: MerossRequest,
    ) -> MerossResponse | None:
        """
        route the request through MQTT or HTTP to the physical device.
        callback will be called on successful replies and actually implemented
        only when HTTPing SET requests. On MQTT we rely on async PUSH and SETACK to manage
        confirmation/status updates
        """
        self.lastrequest = time()
        mqttfailed = False
        if self.curr_protocol is CONF_PROTOCOL_MQTT:
            if self._mqtt_publish:
                if response := await self.async_mqtt_request_raw(request):
                    return response

                mqttfailed = True

            # MQTT not connected or not allowing publishing
            if self.conf_protocol is CONF_PROTOCOL_MQTT:
                return None
            # protocol is AUTO
            self._switch_protocol(CONF_PROTOCOL_HTTP)

        # curr_protocol is HTTP
        if response := await self.async_http_request_raw(request, attempts=3):
            return response

        if (
            self._mqtt_active
            and self._mqtt_publish
            and (self.conf_protocol is CONF_PROTOCOL_AUTO)
            and not mqttfailed
        ):
            return await self.async_mqtt_request_raw(request)

        return None

    async def async_request(
        self,
        namespace: str,
        method: str,
        payload: MerossPayloadType,
    ) -> MerossResponse | None:
        return await self.async_request_raw(
            MerossRequest(self.key, namespace, method, payload, self._topic_response)
        )

    @property
    def tz(self) -> tzinfo:
        tz_name = self.descriptor.timezone
        if not tz_name:
            return timezone.utc
        if self._tzinfo and (self._tzinfo.key == tz_name):
            return self._tzinfo
        try:
            self._tzinfo = ZoneInfo(tz_name)
            return self._tzinfo
        except Exception:
            self.log(
                self.WARNING,
                "Unable to load timezone info for %s - check your python environment",
                tz_name,
                timeout=14400,
            )
            self._tzinfo = None
        return timezone.utc

    def check_device_timezone(self):
        """
        Verifies the device timezone has the same utc offset as HA local timezone.
        This is expecially sensible when the device has 'Consumption' or
        schedules (calendar entities) in order to align device local time to
        what is expected in HA.
        """
        tz_name = self.descriptor.timezone
        if tz_name:
            ha_now = dt_util.now()
            device_now = ha_now.astimezone(self.tz)
            if ha_now.utcoffset() == device_now.utcoffset():
                remove_issue(mlc.ISSUE_DEVICE_TIMEZONE, self.id)
                return

        create_issue(
            mlc.ISSUE_DEVICE_TIMEZONE,
            self.id,
            severity=IssueSeverity.WARNING,
            translation_placeholders={"device_name": self.name},
        )

    def _get_device_info_name_key(self) -> str:
        return mc.KEY_DEVNAME

    def _get_internal_name(self) -> str:
        return self.descriptor.productname

    def _set_offline(self):
        super()._set_offline()
        self._polling_delay = self.polling_period
        self._mqtt_active = self._http_active = None
        for strategy in self.polling_strategies.values():
            strategy.lastrequest = 0

    # interface: self
    @property
    def host(self):
        return self.config.get(CONF_HOST) or self.descriptor.innerIp

    @property
    def mqtt_locallyactive(self):
        """
        reports if the device is actively paired to a private (non-meross) MQTT
        in order to decide if we can/should send over a local MQTT with good
        chances of success.
        we should also check if the _mqtt_connection is 'publishable' but
        at the moment the MerossApi MQTTConnection doesn't allow disabling it
        """
        return self._mqtt_active and not self._mqtt_active.is_cloud_connection

    @property
    def mqtt_broker(self) -> HostAddress:
        # deciding which broker to connect to might prove to be hard
        # since devices might fail-over the mqtt connection between 2 hosts
        if p_debug := self.device_debug:
            # we have 'current' connection info so this should be very trustable
            with self.exception_warning(
                "mqtt_broker - parsing current brokers info", timeout=10
            ):
                p_cloud = p_debug[mc.KEY_CLOUD]
                active_server = p_cloud[mc.KEY_ACTIVESERVER]
                if active_server == p_cloud[mc.KEY_MAINSERVER]:
                    return HostAddress(
                        str(active_server), get_port_safe(p_cloud, mc.KEY_MAINPORT)
                    )
                elif active_server == p_cloud[mc.KEY_SECONDSERVER]:
                    return HostAddress(
                        str(active_server), get_port_safe(p_cloud, mc.KEY_SECONDPORT)
                    )

        fw = self.descriptor.firmware
        return HostAddress(str(fw[mc.KEY_SERVER]), get_port_safe(fw, mc.KEY_PORT))

    def get_device_datetime(self, epoch):
        """
        given the epoch (utc timestamp) returns the datetime
        in device local timezone
        """
        return datetime_from_epoch(epoch, self.tz)

    def register_parser(
        self,
        namespace: str,
        entity: MerossEntity,
        parse_func: typing.Callable[[dict], None] | None = None,
    ):
        if not (handler := self.namespace_handlers.get(namespace)):
            handler = self._create_handler(namespace)
        handler.register(entity, parse_func)

    def unregister_parser(self, namespace: str, entity: MerossEntity):
        if handler := self.namespace_handlers.get(namespace):
            handler.unregister(entity)

    def start(self):
        # called by async_setup_entry after the entities have been registered
        # here we'll register mqtt listening (in case) and start polling after
        # the states have been eventually restored (some entities need this)
        self._check_mqtt_connection_attach()
        self._unsub_polling_callback = schedule_async_callback(
            self.hass, 0, self._async_polling_callback, None
        )

    def entry_option_setup(self, config_schema: dict):
        """
        called when setting up an OptionsFlowHandler to expose
        configurable device preoperties which are stored at the device level
        and not at the configuration/option level
        see derived implementations
        """
        if mc.NS_APPLIANCE_SYSTEM_TIME in self.descriptor.ability:
            global TIMEZONES_SET
            if TIMEZONES_SET is None:
                try:
                    import zoneinfo

                    TIMEZONES_SET = zoneinfo.available_timezones()
                except Exception:
                    pass
                if TIMEZONES_SET:
                    TIMEZONES_SET = vol.In(sorted(TIMEZONES_SET))
                else:
                    # if error or empty try fallback to pytz if avail
                    try:
                        from pytz import common_timezones

                        TIMEZONES_SET = vol.In(sorted(common_timezones))
                    except Exception:
                        TIMEZONES_SET = str
            config_schema[
                vol.Optional(
                    mc.KEY_TIMEZONE,
                    description={"suggested_value": self.descriptor.timezone},
                )
            ] = TIMEZONES_SET

    async def async_entry_option_update(self, user_input: DeviceConfigType):
        """
        called when the user 'SUBMIT' an OptionsFlowHandler: here we'll
        receive the full user_input so to update device config properties
        (this is actually called in sequence with entry_update_listener
        just the latter is async)
        """
        if mc.NS_APPLIANCE_SYSTEM_TIME in self.descriptor.ability:
            timezone = user_input.get(mc.KEY_TIMEZONE)
            if timezone != self.descriptor.timezone:
                if await self.async_config_device_timezone(timezone):
                    # if there's a pending issue, the user might still
                    # use the OptionsFlow to fix stuff so we'll
                    # shut this down anyway..it will reappear in case
                    remove_issue(mlc.ISSUE_DEVICE_TIMEZONE, self.id)

    async def async_bind(
        self, broker: HostAddress, *, key: str | None = None, userid: str | None = None
    ):
        if key is None:
            key = self.key
        if userid is None:
            userid = self.descriptor.userId or ""
        bind = (
            mc.NS_APPLIANCE_CONFIG_KEY,
            mc.METHOD_SET,
            {
                mc.KEY_KEY: {
                    mc.KEY_GATEWAY: {
                        mc.KEY_HOST: broker.host,
                        mc.KEY_PORT: broker.port,
                        mc.KEY_SECONDHOST: broker.host,
                        mc.KEY_SECONDPORT: broker.port,
                        "redirect": 1,
                    },
                    mc.KEY_KEY: key,
                    mc.KEY_USERID: userid,
                }
            },
        )
        # we don't have a clue if it works or not..just go over http
        return await self.async_http_request(*bind)

    async def async_unbind(self):
        """
        WARNING!!!
        Hardware reset to factory default: the device will unpair itself from
        the (cloud) broker and then reboot, ready to be initialized/paired
        """
        unbind = (mc.NS_APPLIANCE_CONTROL_UNBIND, mc.METHOD_PUSH, {})
        # in case we're connected to a cloud broker we'll use that since
        # it appears the broker session level will take care of also removing
        # the device from its list, thus totally cancelling it from the Meross account
        if self._mqtt_publish and self._mqtt_publish.is_cloud_connection:
            return await self.async_mqtt_request(*unbind)
        # else go with whatever transport: the device will reset it's configuration
        return await self.async_request(*unbind)

    async def async_mqtt_request_raw(
        self,
        request: MerossMessage,
    ) -> MerossResponse | None:
        if not self._mqtt_publish:
            # even if we're smart enough to not call async_mqtt_request when no mqtt
            # available, it could happen we loose that when asynchronously coming here
            self.log(
                self.DEBUG,
                "Attempting to use async_mqtt_request with no publishing profile",
            )
            return None
        self._mqtt_lastrequest = time()
        self._trace_or_log(
            self._mqtt_lastrequest,
            request,
            CONF_PROTOCOL_MQTT,
            self.TRACE_TX,
        )
        self._queued_smartpoll_requests += 1
        return await self._mqtt_publish.async_mqtt_publish(self.id, request)

    async def async_mqtt_request(
        self,
        namespace: str,
        method: str,
        payload: MerossPayloadType,
    ):
        return await self.async_mqtt_request_raw(
            MerossRequest(self.key, namespace, method, payload, self._topic_response)
        )

    def mqtt_request_raw(
        self,
        request: MerossRequest,
    ):
        return self.hass.async_create_task(self.async_mqtt_request_raw(request))

    def mqtt_request(
        self,
        namespace: str,
        method: str,
        payload: MerossPayloadType,
    ):
        return self.hass.async_create_task(
            self.async_mqtt_request(namespace, method, payload)
        )

    async def async_http_request_raw(
        self,
        request: MerossRequest,
        attempts: int = 1,
    ) -> MerossResponse | None:
        method = request.method
        namespace = request.namespace
        with self.exception_warning(
            "async_http_request %s %s",
            method,
            namespace,
            timeout=14400,
        ):
            if not (http := self._http):
                http = MerossHttpClient(
                    self.host,  # type: ignore
                    self.key,
                    async_get_clientsession(self.hass),
                )
                self._http = http

            for attempt in range(attempts):
                # since we get 'random' connection errors, this is a retry attempts loop
                # until we get it done. We'd want to break out early on specific events tho (Timeouts)
                self._http_lastrequest = time()
                self._trace_or_log(
                    self._http_lastrequest,
                    request,
                    CONF_PROTOCOL_HTTP,
                    self.TRACE_TX,
                )
                try:
                    response = await http.async_request_message(request)
                    self.device_response_size_min = max(
                        self.device_response_size_min, len(response.json())
                    )
                    break
                except TerminatedException:
                    return None
                except JSONDecodeError as jsonerror:
                    # this could happen when the response carries a truncated payload
                    # and might be due to an 'hard' limit in the capacity of the
                    # device http output buffer (when the response is too long)
                    self.log(
                        self.DEBUG,
                        "HTTP ERROR %s %s (messageId:%s JSONDecodeError:%s attempt:%d)",
                        method,
                        namespace,
                        request.messageid,
                        str(jsonerror),
                        attempt,
                    )
                    response_text = jsonerror.doc
                    response_text_len_safe = int(len(response_text) * 0.9)
                    error_pos = jsonerror.pos
                    if error_pos > response_text_len_safe:
                        # the error happened because of truncated json payload
                        self.device_response_size_max = response_text_len_safe
                        self.device_response_size_min = min(
                            self.device_response_size_min, self.device_response_size_max
                        )
                        self.log(
                            self.DEBUG,
                            "Updating device_response_size_min:%d device_response_size_max:%d",
                            self.device_response_size_min,
                            self.device_response_size_max,
                        )
                        if namespace == mc.NS_APPLIANCE_CONTROL_MULTIPLE:
                            # try to recover by discarding the incomplete
                            # message at the end
                            trunc_pos = response_text.rfind(',{"header":')
                            if trunc_pos != -1:
                                response_text = response_text[0:trunc_pos] + "]}}"
                                response = MerossResponse(response_text)
                                break

                    return None
                except Exception as exception:
                    self.log(
                        self.DEBUG,
                        "HTTP ERROR %s %s (messageId:%s %s:%s attempt:%d)",
                        method,
                        namespace,
                        request.messageid,
                        exception.__class__.__name__,
                        str(exception),
                        attempt,
                    )
                    if not self._online:
                        return None

                    if namespace is mc.NS_APPLIANCE_SYSTEM_ALL:
                        if self._http_active:
                            self._http_active = None
                            self.sensor_protocol.update_attr_inactive(
                                ProtocolSensor.ATTR_HTTP
                            )
                    elif namespace is mc.NS_APPLIANCE_CONTROL_UNBIND:
                        if isinstance(exception, ServerDisconnectedError):
                            # this is expected when issuing the UNBIND
                            # so this is an indication we're dead
                            self._set_offline()
                            return None
                    elif namespace is mc.NS_APPLIANCE_CONTROL_MULTIPLE:
                        if isinstance(exception, ServerDisconnectedError):
                            # this happens (instead of JSONDecodeError)
                            # on my msl120. I guess the (older) fw behaves
                            # differently than those responding incomplete json.
                            # the None response will be managed in the caller
                            # Here we reduce the device_response_size_max so that
                            # next ns_multiple will be less demanding. device_response_size_min
                            # is another dynamic param representing the biggest payload ever received
                            self.device_response_size_max = (
                                self.device_response_size_max
                                + self.device_response_size_min
                            ) / 2
                            self.log(
                                self.DEBUG,
                                "Updating device_response_size_max:%d",
                                self.device_response_size_max,
                            )
                            return None

                    if isinstance(exception, asyncio.TimeoutError):
                        return None
            else:
                return None

            # add a sanity check here since we have some issues (#341)
            # that might be related to misconfigured devices where the
            # host address points to a different device than configured.
            # Our current device.id in fact points (or should) to the uuid discovered
            # in configuration but if by chance the device changes ip and we miss
            # the dynamic change (eitehr dhcp not working or HA down while dhcp updating)
            # we might end up with our configured host pointing to a different device
            # and this might (unluckily) be another Meross with the same key
            # so it could rightly respond here. This shouldnt happen over MQTT
            # since the device.id is being taken care of by the routing mechanism
            if self._check_uuid_mismatch(get_message_uuid(response[mc.KEY_HEADER])):
                return None

            self._http_lastresponse = epoch = time()
            self._trace_or_log(epoch, response, CONF_PROTOCOL_HTTP, self.TRACE_RX)
            if not self._http_active:
                self._http_active = http
                self.sensor_protocol.update_attr_active(ProtocolSensor.ATTR_HTTP)
            if self.curr_protocol is not CONF_PROTOCOL_HTTP:
                if (self.pref_protocol is CONF_PROTOCOL_HTTP) or (
                    not self._mqtt_active
                ):
                    self._switch_protocol(CONF_PROTOCOL_HTTP)
            self._receive(epoch, response)
            return response

        return None

    async def async_http_request(
        self,
        namespace: str,
        method: str,
        payload: MerossPayloadType,
    ):
        return await self.async_http_request_raw(
            MerossRequest(self.key, namespace, method, payload, self._topic_response)
        )

    async def async_request_poll(self, strategy: PollingStrategy):
        if self._multiple_len and (
            strategy.response_size < self.device_response_size_max
        ):
            # device supports NS_APPLIANCE_CONTROL_MULTIPLE namespace
            # so we pack this request
            multiple_response_size = (
                self._multiple_response_size + strategy.response_size
            )
            if multiple_response_size > self.device_response_size_max:
                await self.async_request_flush()
                multiple_response_size = (
                    self._multiple_response_size + strategy.response_size
                )
            self._multiple_requests.append(strategy.request)
            self._multiple_response_size = multiple_response_size
            self._multiple_len -= 1
            if self._multiple_len:
                return
            await self.async_request_flush()
        else:
            await self.async_request(*strategy.request)

    async def async_request_smartpoll(
        self,
        strategy: PollingStrategy,
        epoch: float,
        *,
        cloud_queue_max: int = 1,
    ):
        if (self.curr_protocol is CONF_PROTOCOL_MQTT) and (not self.mqtt_locallyactive):
            # the request would go over cloud mqtt
            if (self._queued_smartpoll_requests >= cloud_queue_max) or (
                (epoch - strategy.lastrequest) < strategy.polling_period_cloud
            ):
                return False
        strategy.lastrequest = epoch
        await self.async_request_poll(strategy)
        return True

    async def async_request_flush(self):
        multiple_requests = self._multiple_requests
        multiple_response_size = self._multiple_response_size
        self._multiple_len = self._multiple_len_max
        self._multiple_requests = []
        self._multiple_response_size = PARAM_HEADER_SIZE

        requests_len = len(multiple_requests)
        while self.online and requests_len:
            if requests_len == 1:
                await self.async_request(*multiple_requests[0])
                return

            if not (
                response := await self.async_request_ack(
                    mc.NS_APPLIANCE_CONTROL_MULTIPLE,
                    mc.METHOD_SET,
                    {
                        mc.KEY_MULTIPLE: [
                            MerossRequest(self.key, *request, self._topic_response)
                            for request in multiple_requests
                        ]
                    },
                )
            ):
                # the ns_multiple failed but the reason could be the device
                # did overflow somehow. I've seen 2 kind of errors so far on the
                # HTTP client: typically the device returns an incomplete json
                # and this is partly recovered in our http interface. One(old)
                # bulb (msl120) instead completely disconnects (ServerDisconnectedException
                # in http client) and so we get here with no response. The same
                # msl bulb timeouts completely on MQTT, so the response to our mqtt requests
                # is None again. At this point, if the device is still online we're
                # trying a last resort issue of single requests
                if self._online:
                    self.log(
                        self.WARNING,
                        "Appliance.Control.Multiple failed with no response: requests=%d expected size=%d",
                        requests_len,
                        multiple_response_size,
                    )
                    for request in multiple_requests:
                        await self.async_request(*request)
                        if not self._online:
                            break
                return

            multiple_responses = response[mc.KEY_PAYLOAD][mc.KEY_MULTIPLE]
            responses_len = len(multiple_responses)
            if self.isEnabledFor(self.DEBUG):
                self.log(
                    self.DEBUG,
                    "Appliance.Control.Multiple requests=%d (responses=%d) expected size=%d (actual=%d)",
                    requests_len,
                    responses_len,
                    multiple_response_size,
                    len(response.json()),
                )
            message: MerossMessageType
            if responses_len == requests_len:
                # faster shortcut
                for message in multiple_responses:
                    self._handle(
                        message[mc.KEY_HEADER],
                        message[mc.KEY_PAYLOAD],
                    )
                return
            # the requests payload was too big and the response was
            # truncated. the http client tried to 'recover' by discarding
            # the incomplete payloads so we'll check what's missing
            for message in multiple_responses:
                m_header = message[mc.KEY_HEADER]
                self._handle(
                    m_header,
                    message[mc.KEY_PAYLOAD],
                )
                namespace = m_header[mc.KEY_NAMESPACE]
                for request in multiple_requests:
                    if request[0] == namespace:
                        multiple_requests.remove(request)
                        break
            # and re-issue the missing ones
            requests_len = len(multiple_requests)
            multiple_response_size = -1  # logging purpose

    async def _async_request_updates(self, epoch: float, namespace: str | None):
        """
        This is a 'versatile' polling strategy called on timer
        or when the device comes online (passing in the received namespace)
        'namespace' is 'None' when we're handling a scheduled polling when
        the device is online. When 'namespace' is not 'None' it represents the event
        of the device coming online following a succesful received message. This is
        likely to be 'NS_ALL', since it's the only message we request when offline.
        If we're connected to an MQTT broker anyway it could be any 'PUSH' message.
        We'll use _queued_smartpoll_requests to track how many polls went through
        over MQTT for this cycle in order to only send 1 for each if we're
        binded to a cloud MQTT broker (in order to reduce bursts).
        If a poll request is discarded because of this, it should go through
        on the next polling cycle. This will 'spread' smart requests over
        subsequent polls
        """
        self._queued_smartpoll_requests = 0
        for _strategy in self.polling_strategies.values():
            if not self._online:
                break  # do not return: do the flush first!
            if namespace != _strategy.namespace:
                await _strategy.async_poll(self, epoch, namespace)
        # needed even if offline: it takes care of resetting the ns_multiple state
        await self.async_request_flush()

    @callback
    async def _async_polling_callback(self, namespace: str):
        self.log(self.DEBUG, "Polling start")
        try:
            self._unsub_polling_callback = None
            epoch = time()

            if self._online:
                # evaluate device availability by checking lastrequest got answered in less than polling_period
                if (self.lastresponse > self.lastrequest) or (
                    (epoch - self.lastrequest) < (self.polling_period - 2)
                ):
                    pass
                # when we 'fall' offline while on MQTT eventually retrigger HTTP.
                # the reverse is not needed since we switch HTTP -> MQTT right-away
                # when HTTP fails (see async_request)
                elif (self.conf_protocol is CONF_PROTOCOL_AUTO) and (
                    self.curr_protocol is not CONF_PROTOCOL_HTTP
                ):
                    self._switch_protocol(CONF_PROTOCOL_HTTP)
                else:
                    self._set_offline()
                    return

                # when mqtt is working as a fallback for HTTP
                # we should periodically check if http comes back
                # in case our self.pref_protocol is HTTP.
                # when self.pref_protocol is MQTT we don't care
                # since we'll just try the switch when mqtt fails
                if (
                    (self.curr_protocol is CONF_PROTOCOL_MQTT)
                    and (self.pref_protocol is CONF_PROTOCOL_HTTP)
                    and ((epoch - self._http_lastrequest) > PARAM_HEARTBEAT_PERIOD)
                ):
                    await self.async_http_request(
                        *get_default_arguments(mc.NS_APPLIANCE_SYSTEM_ALL)
                    )
                    # going on, should the http come online, the next
                    # async_request_updates will be 'smart' again, skipping
                    # state updates coming through mqtt (since we're still
                    # connected) but now requesting over http as preferred

                if self.mqtt_locallyactive:
                    # implement an heartbeat since mqtt might
                    # be unused for quite a bit
                    if (epoch - self._mqtt_lastresponse) > PARAM_HEARTBEAT_PERIOD:
                        if not await self.async_mqtt_request(
                            *get_default_arguments(mc.NS_APPLIANCE_SYSTEM_ALL)
                        ):
                            self._mqtt_active = None
                            self.sensor_protocol.update_attr_inactive(
                                ProtocolSensor.ATTR_MQTT
                            )
                        # going on could eventually try/switch to HTTP
                    elif epoch > self._timezone_next_check:
                        # when on local mqtt we have the responsibility for
                        # setting the device timezone/dst transition times
                        # but this is a process potentially consuming a lot
                        # (checking future DST) so we'll be lazy on this by
                        # scheduling not so often and depending on a bunch of
                        # side conditions (like the device being time-aligned)
                        self._timezone_next_check = (
                            epoch + PARAM_TIMEZONE_CHECK_NOTOK_PERIOD
                        )
                        if self.device_timedelta < PARAM_TIMESTAMP_TOLERANCE:
                            with self.exception_warning("_check_device_timezone"):
                                if self._check_device_timerules():
                                    # timezone trans not good..fix and check again soon
                                    await self.async_config_device_timezone(
                                        self.descriptor.timezone
                                    )
                                else:  # timezone trans good..check again in more time
                                    self._timezone_next_check = (
                                        epoch + PARAM_TIMEZONE_CHECK_OK_PERIOD
                                    )

                await self._async_request_updates(epoch, namespace)

            else:  # offline
                ns_all = get_default_arguments(mc.NS_APPLIANCE_SYSTEM_ALL)
                if self.conf_protocol is CONF_PROTOCOL_AUTO:
                    if self.host:
                        await self.async_http_request(*ns_all)
                    if self._mqtt_publish and not self._online:
                        await self.async_mqtt_request(*ns_all)
                elif self.conf_protocol is CONF_PROTOCOL_MQTT:
                    if self._mqtt_publish:
                        await self.async_mqtt_request(*ns_all)
                else:  # self.conf_protocol is CONF_PROTOCOL_HTTP:
                    await self.async_http_request(*ns_all)

                if self._online:
                    await self._async_request_updates(epoch, mc.NS_APPLIANCE_SYSTEM_ALL)
                else:
                    if self._polling_delay < PARAM_HEARTBEAT_PERIOD:
                        self._polling_delay += self.polling_period
                    else:
                        self._polling_delay = PARAM_HEARTBEAT_PERIOD
        finally:
            self._unsub_polling_callback = schedule_async_callback(
                self.hass, self._polling_delay, self._async_polling_callback, None
            )
            self.log(self.DEBUG, "Polling end")

    def mqtt_receive(self, message: MerossResponse):
        assert self._mqtt_connected and (self.conf_protocol is not CONF_PROTOCOL_HTTP)
        self._mqtt_lastresponse = epoch = time()
        self._trace_or_log(epoch, message, CONF_PROTOCOL_MQTT, self.TRACE_RX)
        if not self._mqtt_active:
            self._mqtt_active = self._mqtt_connected
            if self._online:
                self.sensor_protocol.update_attr_active(ProtocolSensor.ATTR_MQTT)
        if self.curr_protocol is not CONF_PROTOCOL_MQTT:
            if (self.pref_protocol is CONF_PROTOCOL_MQTT) or (not self._http_active):
                self._switch_protocol(CONF_PROTOCOL_MQTT)
        self._receive(epoch, message)

    def mqtt_attached(self, mqtt_connection: MQTTConnection):
        self.log(
            self.DEBUG,
            "mqtt_attached to %s",
            self.loggable_broker(mqtt_connection.broker),
        )
        self._mqtt_connection = mqtt_connection
        self._topic_response = mqtt_connection.topic_response
        if mqtt_connection.mqtt_is_connected:
            self.mqtt_connected()

    def mqtt_detached(self):
        assert self._mqtt_connection
        self.log(
            self.DEBUG,
            "mqtt_detached from %s",
            self.loggable_broker(self._mqtt_connection.broker),
        )
        if self._mqtt_connected:
            self.mqtt_disconnected()
        self._mqtt_connection = None

    def mqtt_connected(self):
        _mqtt_connection = self._mqtt_connection
        assert _mqtt_connection
        self.log(
            self.DEBUG,
            "mqtt_connected to %s",
            self.loggable_broker(_mqtt_connection.broker),
        )
        self._mqtt_connected = _mqtt_connection
        if _mqtt_connection.allow_mqtt_publish:
            self._mqtt_publish = _mqtt_connection
            if not self._online and self._unsub_polling_callback:
                # reschedule immediately
                self._unsub_polling_callback.cancel()
                self._unsub_polling_callback = schedule_async_callback(
                    self.hass, 0, self._async_polling_callback, None
                )

        elif self.conf_protocol is CONF_PROTOCOL_MQTT:
            self.log(
                self.WARNING,
                "MQTT connection doesn't allow publishing - device will not be able send commands",
                timeout=14400,
            )
        self.sensor_protocol.update_attr_active(ProtocolSensor.ATTR_MQTT_BROKER)

    def mqtt_disconnected(self):
        assert self._mqtt_connection
        self.log(
            self.DEBUG,
            "mqtt_disconnected from %s",
            self.loggable_broker(self._mqtt_connection.broker),
        )
        self._mqtt_connected = self._mqtt_publish = self._mqtt_active = None
        if self.curr_protocol is CONF_PROTOCOL_MQTT:
            if self.conf_protocol is CONF_PROTOCOL_AUTO:
                self._switch_protocol(CONF_PROTOCOL_HTTP)
                return
            # conf_protocol should be CONF_PROTOCOL_MQTT:
            elif self._online:
                self._set_offline()
                return
        # run this at the end so it will not double flush
        self.sensor_protocol.update_attrs_inactive(
            ProtocolSensor.ATTR_MQTT_BROKER, ProtocolSensor.ATTR_MQTT
        )

    def profile_linked(self, profile: ApiProfile):
        if self._profile is not profile:
            self.log(
                self.DEBUG,
                "linked to profile:%s",
                self.loggable_profile_id(profile.id),
            )
            if self._mqtt_connection:
                self._mqtt_connection.detach(self)
            if self._profile:
                self._profile.unlink(self)
            self._profile = profile
            self._check_mqtt_connection_attach()

    def profile_unlinked(self):
        assert self._profile
        self.log(
            self.DEBUG,
            "unlinked from profile:%s",
            self.loggable_profile_id(self._profile.id),
        )
        if self._mqtt_connection:
            self._mqtt_connection.detach(self)
        self._profile = None

    def _check_mqtt_connection_attach(self):
        _profile = self._profile
        _mqtt_connection = self._mqtt_connection

        if self.conf_protocol is CONF_PROTOCOL_AUTO:
            # When using CONF_PROTOCOL_AUTO we try to use our 'preferred' (pref_protocol)
            # and eventually fallback (curr_protocol) until some good news allow us
            # to retry pref_protocol. When binded to a cloud_profile always prefer
            # 'local' http since it should be faster and less prone to cloud 'issues'
            if self.config.get(CONF_HOST) or (_profile and _profile.id):
                self.pref_protocol = CONF_PROTOCOL_HTTP
                if self.curr_protocol is not CONF_PROTOCOL_HTTP and self._http_active:
                    self._switch_protocol(CONF_PROTOCOL_HTTP)
            else:
                self.pref_protocol = CONF_PROTOCOL_MQTT
                if self.curr_protocol is not CONF_PROTOCOL_MQTT and self._mqtt_active:
                    self._switch_protocol(CONF_PROTOCOL_MQTT)
        else:
            self.pref_protocol = self.conf_protocol
            if self.curr_protocol is not self.pref_protocol:
                self._switch_protocol(self.pref_protocol)

        if self.conf_protocol is CONF_PROTOCOL_HTTP:
            # strictly HTTP so detach MQTT in case
            if _mqtt_connection:
                _mqtt_connection.detach(self)
            self.polling_strategies.pop(mc.NS_APPLIANCE_SYSTEM_DEBUG, None)
        else:
            if _profile and (self.conf_protocol is CONF_PROTOCOL_AUTO):
                if mc.NS_APPLIANCE_SYSTEM_DEBUG not in self.polling_strategies:
                    SystemDebugPollingStrategy(self, mc.NS_APPLIANCE_SYSTEM_DEBUG)
            else:
                self.polling_strategies.pop(mc.NS_APPLIANCE_SYSTEM_DEBUG, None)

            if _mqtt_connection:
                if _mqtt_connection.profile == _profile:
                    return
                _mqtt_connection.detach(self)

            if _profile:
                _profile.attach_mqtt(self)
            else:
                # this will cause 1 level recursion by
                # calling profile_linked. In general, devices
                # are attached right when loaded (by default they're attached to MerossApi
                # if no CloudProfile matches). Whenever a Cloud profile appears, it can
                # steal the device from another ApiProfile (and this should be safe).
                # but when a cloud profile is unloaded, it unlinks its devices which will
                # rest without an ApiProfile. This is still to be fixed but at least,
                # whenever we refresh the device config, this kind of 'failover' will
                # definitely bind the device to the local broker in case it got orphaned
                self.api.try_link(self)

    def _receive(self, epoch: float, message: MerossResponse):
        """
        default (received) message handling entry point
        """
        self.lastresponse = epoch
        header = message[mc.KEY_HEADER]
        # we'll use the device timestamp to 'align' our time to the device one
        # this is useful for metered plugs reporting timestamped energy consumption
        # and we want to 'translate' this timings in our (local) time.
        # We ignore delays below PARAM_TIMESTAMP_TOLERANCE since
        # we'll always be a bit late in processing
        self.device_timestamp = header[mc.KEY_TIMESTAMP]
        device_timedelta = epoch - self.device_timestamp
        if abs(device_timedelta) > PARAM_TIMESTAMP_TOLERANCE:
            if (
                abs(self.device_timedelta - device_timedelta)
                > PARAM_TIMESTAMP_TOLERANCE
            ):
                # big step so we're not averaging
                self.device_timedelta = device_timedelta
            else:  # average the sampled timedelta
                self.device_timedelta = (
                    4 * self.device_timedelta + device_timedelta
                ) / 5
            self._config_device_timestamp(epoch)
        else:
            self.device_timedelta = 0

        if self.isEnabledFor(self.DEBUG):
            # it appears sometimes the devices
            # send an incorrect signature hash
            # but at the moment this is unlikely to be critical
            sign = get_message_signature(
                header[mc.KEY_MESSAGEID], self.key, header[mc.KEY_TIMESTAMP]
            )
            if sign != header[mc.KEY_SIGN]:
                self.log(
                    self.DEBUG,
                    "Received signature error: computed=%s, header=%s",
                    sign,
                    json_dumps(header),  # TODO: obfuscate header? check
                )

        if not self._online:
            self._set_online()
            self._polling_delay = self.polling_period
            self.sensor_protocol.update_connected()
            # retrigger the polling loop in case it is scheduled/pending.
            # This could happen when we receive an MQTT message
            if self._unsub_polling_callback:
                self._unsub_polling_callback.cancel()
                self._unsub_polling_callback = schedule_async_callback(
                    self.hass,
                    0,
                    self._async_polling_callback,
                    header[mc.KEY_NAMESPACE],
                )

        return self._handle(header, message[mc.KEY_PAYLOAD])

    def _handle(
        self,
        header: MerossHeaderType,
        payload: MerossPayloadType,
    ):
        namespace = header[mc.KEY_NAMESPACE]
        method = header[mc.KEY_METHOD]
        if method == mc.METHOD_ERROR:
            if payload.get(mc.KEY_ERROR) == mc.ERROR_INVALIDKEY:
                self.log(
                    self.WARNING,
                    "Key error: the configured device key is wrong",
                    timeout=14400,
                )
            else:
                self.log(
                    self.WARNING,
                    "Protocol error: namespace:%s payload:%s",
                    namespace,
                    str(self.loggable_dict(payload)),
                    timeout=14400,
                )
            return
        elif method == mc.METHOD_SETACK:
            # SETACK generally doesn't carry any state/info so it is
            # no use parsing..moreover, our callbacks system is full
            # in place so we have no need to further process
            return

        if not (handler := self.namespace_handlers.get(namespace)):
            handler = self._create_handler(namespace)

        handler.lastrequest = self.lastresponse  # type: ignore
        handler.handler(header, payload)  # type: ignore

    def _create_handler(self, namespace: str):
        """Called by the base device message parsing chain when a new
        NamespaceHandler need to be defined (This happens the first time
        the namespace enters the message handling flow)"""
        return NamespaceHandler(self, namespace)

    def _handle_undefined(self, header: MerossHeaderType, payload: MerossPayloadType):
        self.log(
            self.DEBUG,
            "Handler undefined for method:%s namespace:%s payload:%s",
            header[mc.KEY_METHOD],
            header[mc.KEY_NAMESPACE],
            str(self.loggable_dict(payload)),
        )

    def _handle_Appliance_Control_Bind(self, header: dict, payload: dict):
        # already processed by the MQTTConnection session manager
        pass

    def _handle_Appliance_System_Ability(self, header: dict, payload: dict):
        # This should only be requested when we want to update a config_entry
        # (needsave == True) due to a detected fw change or whatever in NS_ALL
        # Before saving, we're checking the abilities did (or didn't) change too
        # If abilities were changed since our init (due to a device fw update likely)
        # we'll reload the config entry because a lot of initialization depends
        # on this and it's hard to change it 'on the fly'
        # This is, overall, an async transaction so we're prepared for
        # this message coming in even when requested from other transactions
        # like device identification or service (meross_lan.request) invocation
        descr = self.descriptor
        oldability = descr.ability
        newability: dict = payload[mc.KEY_ABILITY]
        if oldability != newability:
            self.needsave = True
            oldabilities = oldability.keys()
            newabilities = newability.keys()
            self.log(
                self.WARNING,
                "Trying schedule device configuration reload since the abilities changed (added:%s - removed:%s)",
                str(newabilities - oldabilities),
                str(oldabilities - newabilities),
            )
            self.schedule_entry_reload()

        if self.needsave:
            self.needsave = False
            with self.exception_warning("ConfigEntry update"):
                entries = self.hass.config_entries
                if entry := entries.async_get_entry(self.config_entry_id):
                    data = dict(entry.data)
                    data[CONF_TIMESTAMP] = time()  # force ConfigEntry update..
                    data[CONF_PAYLOAD][mc.KEY_ALL] = descr.all
                    data[CONF_PAYLOAD][mc.KEY_ABILITY] = newability
                    entries.async_update_entry(entry, data=data)

    def _handle_Appliance_System_All(self, header: dict, payload: dict):
        # see issue #341. In case we receive a formally correct response from a
        # mismatched device we should stop everything and obviously don't update our
        # ConfigEntry. Here we check first the identity of the device sending this payload
        # in order to not mess our configuration. All in all this check should be not
        # needed since the only reasonable source of 'device mismatch' is the HTTP protocol
        # which is already guarded in our async_http_request
        if self._check_uuid_mismatch(
            payload[mc.KEY_ALL][mc.KEY_SYSTEM][mc.KEY_HARDWARE][mc.KEY_UUID]
        ):
            return
        else:
            remove_issue(mlc.ISSUE_DEVICE_ID_MISMATCH, self.id)

        descr = self.descriptor
        oldfirmware = descr.firmware
        oldtimezone = descr.timezone
        descr.update(payload)

        if oldtimezone != descr.timezone:
            self.needsave = True

        if oldfirmware != descr.firmware:
            self.needsave = True
            if update_firmware := self.update_firmware:
                # self.update_firmware is dynamically created only when the cloud api
                # reports a newer fw
                update_firmware._attr_installed_version = descr.firmwareVersion
                update_firmware.flush_state()

        if self.conf_protocol is CONF_PROTOCOL_AUTO:
            if self._mqtt_active:
                if not is_device_online(descr.system):
                    self._mqtt_active = None
                    self.sensor_protocol.update_attr_inactive(ProtocolSensor.ATTR_MQTT)
            elif (_mqtt_connected := self._mqtt_connected) and is_device_online(
                descr.system
            ):
                if _mqtt_connected.broker.host == self.mqtt_broker.host:
                    self._mqtt_active = _mqtt_connected
                    self.sensor_protocol.update_attr_active(ProtocolSensor.ATTR_MQTT)
                    # this code path actually only happens when we're working on HTTP so we
                    # skip/optimize the checks (but still on the safe side)
                    if self.curr_protocol is not self.pref_protocol:
                        self._switch_protocol(self.pref_protocol)

        for _key, _digest in descr.digest.items():
            if _parse := getattr(self, f"_parse_{_key}", None):
                _parse(_digest)
        # older firmwares (MSS110 with 1.1.28) look like
        # carrying 'control' instead of 'digest'
        if isinstance(p_control := descr.all.get(mc.KEY_CONTROL), dict):
            for _key, _control in p_control.items():
                if _parse := getattr(self, f"_parse_{_key}", None):
                    _parse(_control)

        if self.needsave:
            # fw update or whatever might have modified the device abilities.
            # we refresh the abilities list before saving the new config_entry
            self.request(get_default_arguments(mc.NS_APPLIANCE_SYSTEM_ABILITY))

    def _handle_Appliance_System_Clock(self, header: dict, payload: dict):
        # already processed by the MQTTConnection session manager
        pass

    def _handle_Appliance_System_Debug(self, header: dict, payload: dict):
        self.device_debug = p_debug = payload[mc.KEY_DEBUG]
        self.sensor_signal_strength.update_state(p_debug[mc.KEY_NETWORK][mc.KEY_SIGNAL])

    def _handle_Appliance_System_DNDMode(self, header: dict, payload: dict):
        self.entity_dnd.update_onoff(payload[mc.KEY_DNDMODE][mc.KEY_MODE])

    def _handle_Appliance_System_Online(self, header: dict, payload: dict):
        # already processed by the MQTTConnection session manager
        pass

    def _handle_Appliance_System_Report(self, header: dict, payload: dict):
        # No clue: sent (MQTT PUSH) by the device on initial connection
        pass

    def _handle_Appliance_System_Runtime(self, header: dict, payload: dict):
        self.sensor_signal_strength.update_state(payload[mc.KEY_RUNTIME][mc.KEY_SIGNAL])

    def _handle_Appliance_System_Time(self, header: dict, payload: dict):
        if header[mc.KEY_METHOD] == mc.METHOD_PUSH:
            self.descriptor.update_time(payload[mc.KEY_TIME])

    def _config_device_timestamp(self, epoch):
        if self.mqtt_locallyactive and (
            mc.NS_APPLIANCE_SYSTEM_CLOCK in self.descriptor.ability
        ):
            # only deal with time related settings when devices are un-paired
            # from the meross cloud
            last_config_delay = epoch - self.device_timedelta_config_epoch
            if last_config_delay > 1800:
                # 30 minutes 'cooldown' in order to avoid restarting
                # the procedure too often
                self.mqtt_request(mc.NS_APPLIANCE_SYSTEM_CLOCK, mc.METHOD_PUSH, {})
                self.device_timedelta_config_epoch = epoch
                return
            if last_config_delay < 30:
                # 30 sec 'deadzone' where we allow the timestamp
                # transaction to complete (should really be like few seconds)
                return
        if (epoch - self.device_timedelta_log_epoch) > 604800:  # 1 week lockout
            self.device_timedelta_log_epoch = epoch
            self.log(
                self.WARNING,
                "Incorrect timestamp: %d seconds behind HA",
                int(self.device_timedelta),
            )

    def _check_device_timerules(self) -> bool:
        """
        verify the data about DST changes in the configured timezone are ok by checking
        the "time" key in the Appliance.System.All payload:
        "time": {
            "timestamp": 1560670665,
            "timezone": "Australia/Sydney",
            "timeRule": [
                [1554566400,36000,0],
                [1570291200,39600,1],
                ...
            ]
        }
        returns True in case we need to fix the device configuration
        see https://github.com/arandall/meross/blob/main/doc/protocol.md#appliancesystemtime
        """
        timestamp = self.device_timestamp  # we'll check against its own timestamp
        time = self.descriptor.time
        timerules: list = time.get(mc.KEY_TIMERULE, [])
        timezone = time.get(mc.KEY_TIMEZONE)
        if timezone:
            # assume "timeRule" entries are ordered on epoch(s)
            # timerule: [1554566400,36000,0] -> [epoch, utcoffset, isdst]
            if not timerules:
                # array empty?
                return True

            def _get_epoch(_timerule: list):
                return _timerule[0]

            idx = bisect.bisect_right(timerules, timestamp, key=_get_epoch)
            if idx == 0:
                # epoch is not (yet) covered in timerules
                return True

            timerule = timerules[idx - 1]  # timerule in effect at the 'epoch'
            device_tzinfo = self.tz

            def _check_incorrect_timerule(_epoch, _timerule):
                _device_datetime = datetime_from_epoch(_epoch, device_tzinfo)
                _utcoffset = device_tzinfo.utcoffset(_device_datetime)
                if _timerule[1] != (_utcoffset.seconds if _utcoffset else 0):
                    return True
                _dstoffset = device_tzinfo.dst(_device_datetime)
                return _timerule[2] != (1 if _dstoffset else 0)

            if _check_incorrect_timerule(timestamp, timerule):
                return True
            # actual device time is covered but we also check if the device timerules
            # are ok in the near future
            timestamp_future = timestamp + PARAM_TIMEZONE_CHECK_OK_PERIOD
            # we have to search (again) in the timerules but we do some
            # short-circuit checks to see if epoch_future is still
            # contained in current timerule
            if idx == len(timerules):
                # timerule is already the last in the list so it will be the only active
                # from now on
                pass
            else:
                timerule_next = timerules[idx]
                timestamp_next = timerule_next[0]
                if timestamp_future >= timestamp_next:
                    # the next timerule will take over
                    # so we check if the transition time set in the device
                    # is correct with the tz database
                    if _check_incorrect_timerule(timestamp_next - 1, timerule):
                        return True
                    if _check_incorrect_timerule(timestamp_next + 1, timerule_next):
                        return True
                    # transition set in timerule_next is coming soon
                    # and will be ok
                    return False

            if _check_incorrect_timerule(timestamp_future, timerule):
                return True

        else:
            # no timezone set in the device so we'd expect an empty timerules
            if timerules:
                return True

        return False

    async def async_config_device_timezone(self, tzname: str | None):
        # assert self.mqtt_locallyactive
        timestamp = self.device_timestamp
        timerules = []
        if tzname:
            """
            we'll look through the list of transition times for current tz
            and provide the actual (last past daylight) and the next to the
            appliance so it knows how and when to offset utc to localtime
            """
            try:
                try:
                    import pytz

                    tz_local = pytz.timezone(tzname)
                    if isinstance(tz_local, pytz.tzinfo.DstTzInfo):
                        idx = bisect.bisect_right(
                            tz_local._utc_transition_times,  # type: ignore
                            datetime.utcfromtimestamp(timestamp),
                        )
                        # idx would be the next transition offset index
                        _transition_info = tz_local._transition_info[idx - 1]  # type: ignore
                        timerules.append(
                            [
                                int(tz_local._utc_transition_times[idx - 1].timestamp()),  # type: ignore
                                int(_transition_info[0].total_seconds()),
                                1 if _transition_info[1].total_seconds() else 0,
                            ]
                        )
                        _transition_info = tz_local._transition_info[idx]  # type: ignore
                        timerules.append(
                            [
                                int(tz_local._utc_transition_times[idx].timestamp()),  # type: ignore
                                int(_transition_info[0].total_seconds()),
                                1 if _transition_info[1].total_seconds() else 0,
                            ]
                        )
                    elif isinstance(tz_local, pytz.tzinfo.StaticTzInfo):
                        timerules = [[0, tz_local.utcoffset(None), 0]]

                except Exception as e:
                    self.log(
                        self.WARNING,
                        "Error(%s) while using pytz to build timezone(%s) ",
                        str(e),
                        tzname,
                    )
                    # if pytx fails we'll fall-back to some euristics
                    device_tzinfo = ZoneInfo(tzname)
                    device_datetime = datetime_from_epoch(timestamp, device_tzinfo)
                    utcoffset = device_tzinfo.utcoffset(device_datetime)
                    utcoffset = utcoffset.seconds if utcoffset else 0
                    isdst = device_tzinfo.dst(device_datetime)
                    timerules = [[timestamp, utcoffset, 1 if isdst else 0]]

            except Exception as e:
                self.log(
                    self.WARNING,
                    "Error(%s) while building timezone(%s) info for %s",
                    str(e),
                    tzname,
                    mc.NS_APPLIANCE_SYSTEM_TIME,
                )
                timerules = [
                    [0, 0, 0],
                    [timestamp + PARAM_TIMEZONE_CHECK_OK_PERIOD, 0, 1],
                ]

        else:
            tzname = ""

        return await self.async_request_ack(
            mc.NS_APPLIANCE_SYSTEM_TIME,
            mc.METHOD_SET,
            payload={
                mc.KEY_TIME: {
                    mc.KEY_TIMEZONE: tzname,
                    mc.KEY_TIMERULE: timerules,
                }
            },
        )

    def _switch_protocol(self, protocol):
        self.log(
            self.DEBUG,
            "Switching protocol to %s",
            protocol,
        )
        self.curr_protocol = protocol
        if self._online:
            self.sensor_protocol.update_connected()

    def _update_config(self):
        """
        common properties caches, read from ConfigEntry on __init__ or when a configentry updates
        """
        config = self.config
        self.conf_protocol = CONF_PROTOCOL_OPTIONS.get(
            config.get(CONF_PROTOCOL), CONF_PROTOCOL_AUTO
        )
        self.polling_period = (
            config.get(CONF_POLLING_PERIOD) or CONF_POLLING_PERIOD_DEFAULT
        )
        if self.polling_period < CONF_POLLING_PERIOD_MIN:
            self.polling_period = CONF_POLLING_PERIOD_MIN
        self._polling_delay = self.polling_period

    def _check_uuid_mismatch(self, response_uuid: str):
        """when detecting a wrong uuid from a response we offline the device"""
        if response_uuid != self.id:
            # here we're not obfuscating device uuid since we might have an hard time identifying the bogus one
            self.log(
                self.CRITICAL,
                "Received a response from a mismatching device (received uuid:%s, configured uuid:%s)",
                response_uuid,
                self.id,
                timeout=900,
            )
            if self._online:
                self._set_offline()
            create_issue(
                mlc.ISSUE_DEVICE_ID_MISMATCH,
                self.id,
                severity=IssueSeverity.CRITICAL,
                translation_placeholders={"device_name": self.name},
            )
            return True
        return False

    def update_latest_version(self, latest_version: LatestVersionType):
        if not (update_firmware := self.update_firmware):
            self.update_firmware = update_firmware = MLUpdate(self)
        update_firmware._attr_installed_version = self.descriptor.firmwareVersion
        update_firmware._attr_latest_version = latest_version.get(mc.KEY_VERSION)
        update_firmware._attr_release_summary = latest_version.get(mc.KEY_DESCRIPTION)
        update_firmware.flush_state()

    @callback
    def _trace_ability(self, abilities_iterator: typing.Iterator[str]):
        try:
            # avoid interleave tracing ability with polling loop
            # also, since we could trigger this at early stages
            # in device init, this check will prevent iterating
            # at least until the device fully initialize through
            # self.start()
            if self._unsub_polling_callback:
                while self.online:
                    ability = next(abilities_iterator)
                    if ability not in TRACE_ABILITY_EXCLUDE:
                        self.request(get_default_arguments(ability))
                        break
            self._unsub_trace_ability_callback = schedule_callback(
                self.hass,
                PARAM_TRACING_ABILITY_POLL_TIMEOUT,
                self._trace_ability,
                abilities_iterator,
            )
        except Exception:  # finished ?!
            self._unsub_trace_ability_callback = None

    def _trace_or_log(
        self,
        epoch: float,
        message: MerossMessage,
        protocol: str,
        rxtx: str,
    ):
        if self.trace_file:
            header = message[mc.KEY_HEADER]
            self.trace(
                epoch,
                message[mc.KEY_PAYLOAD],
                header[mc.KEY_NAMESPACE],
                header[mc.KEY_METHOD],
                protocol,
                rxtx,
            )
        elif self.isEnabledFor(self.VERBOSE):
            header = message[mc.KEY_HEADER]
            self.log(
                self.VERBOSE,
                "%s(%s) %s %s (messageId:%s) %s",
                rxtx,
                protocol,
                header[mc.KEY_METHOD],
                header[mc.KEY_NAMESPACE],
                header[mc.KEY_MESSAGEID],
                json_dumps(self.loggable_dict(message)),
            )
        elif self.isEnabledFor(self.DEBUG):
            header = message[mc.KEY_HEADER]
            self.log(
                self.DEBUG,
                "%s(%s) %s %s (messageId:%s)",
                rxtx,
                protocol,
                header[mc.KEY_METHOD],
                header[mc.KEY_NAMESPACE],
                header[mc.KEY_MESSAGEID],
            )
