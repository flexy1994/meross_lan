"""
    meross_lan module interface to access Meross Cloud services
"""
from __future__ import annotations

import abc
from contextlib import contextmanager
from json import dumps as json_dumps, loads as json_loads
from logging import DEBUG, INFO
from time import time
import typing

from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY
from homeassistant.core import callback
from homeassistant.helpers import storage
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import paho.mqtt.client as mqtt

from .const import (
    CONF_ALLOW_MQTT_PUBLISH,
    CONF_CREATE_DIAGNOSTIC_ENTITIES,
    CONF_DEVICE_ID,
    CONF_KEY,
    CONF_PAYLOAD,
    CONF_PROFILE_ID_LOCAL,
    DOMAIN,
    PARAM_CLOUDPROFILE_DELAYED_SAVE_TIMEOUT,
    PARAM_CLOUDPROFILE_QUERY_DEVICELIST_TIMEOUT,
    PARAM_UNAVAILABILITY_TIMEOUT,
    ProfileConfigType,
)
from .helpers import (
    LOGGER,
    ApiProfile,
    ConfigEntriesHelper,
    Loggable,
    datetime_from_epoch,
    schedule_async_callback,
    schedule_callback,
)
from .meross_device_hub import MerossDeviceHub
from .merossclient import (
    MEROSSDEBUG,
    KeyType,
    build_payload,
    const as mc,
    get_default_arguments,
    get_namespacekey,
    get_replykey,
)
from .merossclient.cloudapi import (
    APISTATUS_TOKEN_ERRORS,
    CloudApiError,
    MerossCloudCredentials,
    MerossMQTTClient,
    async_cloudapi_device_devlist,
    async_cloudapi_hub_getsubdevices,
    async_cloudapi_logout,
    generate_app_id,
    parse_domain,
)

if typing.TYPE_CHECKING:
    import asyncio
    from typing import Final

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from . import MerossApi
    from .meross_device import MerossDevice
    from .merossclient.cloudapi import DeviceInfoType, SubDeviceInfoType

    UuidType = str
    DeviceInfoDictType = dict[UuidType, DeviceInfoType]


class MQTTConnection(Loggable):
    """
    Base abstract class representing a connection to an MQTT
    broker. Historically, MQTT support was only through MerossApi
    and the HA core MQTT broker. The introduction of Meross cloud
    connection has 'generalized' the concept of the MQTT broker.
    This interface is used by devices to actually send/receive
    MQTT messages (in place of the legacy approach using MerossApi)
    and represents a link to a broker (either through HA or a
    merosss cloud mqtt)
    """

    _KEY_STARTTIME = "__starttime"
    _KEY_REQUESTTIME = "__requesttime"
    _KEY_REQUESTCOUNT = "__requestcount"

    __slots__ = (
        "profile",
        "broker",
        "mqttdevices",
        "mqttdiscovering",
        "_mqtt_is_connected",
        "_unsub_discovery_callback",
    )

    def __init__(
        self,
        profile: MerossCloudProfile | MerossApi,
        connection_id: str,
        broker: tuple[str, int],
    ):
        super().__init__(connection_id)
        self.profile = profile
        self.broker = broker
        self.mqttdevices: dict[str, MerossDevice] = {}
        self.mqttdiscovering: dict[str, dict] = {}
        self._mqtt_is_connected = False
        self._unsub_discovery_callback: asyncio.TimerHandle | None = None

    async def async_shutdown(self):
        if self._unsub_discovery_callback:
            self._unsub_discovery_callback.cancel()
            self._unsub_discovery_callback = None
        self.mqttdiscovering.clear()
        for device in self.mqttdevices.values():
            device.mqtt_detached()
        self.mqttdevices.clear()

    @property
    def allow_mqtt_publish(self):
        return self.profile.allow_mqtt_publish

    def attach(self, device: MerossDevice):
        assert device.id not in self.mqttdevices
        self.mqttdevices[device.id] = device
        device.mqtt_attached(self)

    def detach(self, device: MerossDevice):
        assert device.id in self.mqttdevices
        device.mqtt_detached()
        self.mqttdevices.pop(device.id)

    @abc.abstractmethod
    def mqtt_publish(
        self,
        device_id: str,
        namespace: str,
        method: str,
        payload: dict,
        key: KeyType = None,
        messageid: str | None = None,
    ) -> asyncio.Future:
        """
        throw and forget..usually schedules to a background task since
        the actual mqtt send could be sync/blocking
        """
        raise NotImplementedError()

    @abc.abstractmethod
    async def async_mqtt_publish(
        self,
        device_id: str,
        namespace: str,
        method: str,
        payload: dict,
        key: KeyType = None,
        messageid: str | None = None,
    ):
        """
        awaits message publish in asyncio style
        """
        raise NotImplementedError()

    async def async_mqtt_message(self, msg):
        with self.exception_warning("async_mqtt_message"):
            message = json_loads(msg.payload)
            header = message[mc.KEY_HEADER]
            device_id = header[mc.KEY_FROM].split("/")[2]
            if LOGGER.isEnabledFor(DEBUG):
                self.log(
                    DEBUG,
                    "MQTT RECV device_id:(%s) method:(%s) namespace:(%s)",
                    device_id,
                    header[mc.KEY_METHOD],
                    header[mc.KEY_NAMESPACE],
                )
            if device_id in self.mqttdevices:
                self.mqttdevices[device_id].mqtt_receive(
                    header, message[mc.KEY_PAYLOAD]
                )
                return

            if device := ApiProfile.devices.get(device_id):
                # we have the device registered but somehow it is not 'mqtt binded'
                # either it's configuration is ONLY_HTTP or it is paired to the
                # Meross cloud. In this case we shouldn't receive 'local' MQTT
                self.warning(
                    "device(%s) not registered for MQTT handling on this profile",
                    device.name,
                    timeout=14400,
                )
                return

            # lookout for any disabled/ignored entry
            config_entries_helper = ConfigEntriesHelper(ApiProfile.hass)
            if (
                (self.id is CONF_PROFILE_ID_LOCAL)
                and (not config_entries_helper.get_config_entry(DOMAIN))
                and (not config_entries_helper.get_config_flow(DOMAIN))
            ):
                # not really needed but we would like to always have the
                # MQTT hub entry in case so if the user removed that..retrigger
                await ApiProfile.hass.config_entries.flow.async_init(
                    DOMAIN,
                    context={"source": "hub"},
                    data=None,
                )

            if config_entry := config_entries_helper.get_config_entry(device_id):
                # entry already present...skip discovery
                self.log(
                    INFO,
                    "ignoring MQTT discovery for already configured device_id: %s (ConfigEntry is %s)",
                    device_id,
                    "disabled"
                    if config_entry.disabled_by
                    else "ignored"
                    if config_entry.source == "ignore"
                    else "unknown",
                    timeout=14400,  # type: ignore
                )
                return

            # also skip discovered integrations waiting in HA queue
            if config_entries_helper.get_config_flow(device_id):
                self.log(
                    DEBUG,
                    "ignoring discovery for device_id: %s (ConfigFlow is in progress)",
                    device_id,
                    timeout=14400,  # type: ignore
                )
                return

            key = self.profile.key
            if get_replykey(header, key) is not key:
                self.warning(
                    "discovery key error for device_id: %s",
                    device_id,
                    timeout=300,
                )
                if key is not None:
                    return

            discovered = self.get_or_set_discovering(device_id)
            if header[mc.KEY_METHOD] == mc.METHOD_GETACK:
                namespace = header[mc.KEY_NAMESPACE]
                if namespace in (
                    mc.NS_APPLIANCE_SYSTEM_ALL,
                    mc.NS_APPLIANCE_SYSTEM_ABILITY,
                ):
                    discovered.update(message[mc.KEY_PAYLOAD])

            if await self._async_progress_discovery(discovered, device_id):
                return

            self.mqttdiscovering.pop(device_id)
            discovered.pop(MQTTConnection._KEY_REQUESTTIME, None)
            discovered.pop(MQTTConnection._KEY_STARTTIME, None)
            discovered.pop(MQTTConnection._KEY_REQUESTCOUNT, None)
            await ApiProfile.hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_INTEGRATION_DISCOVERY},
                data={
                    CONF_DEVICE_ID: device_id,
                    CONF_PAYLOAD: discovered,
                    CONF_KEY: key,
                },
            )

    @property
    def mqtt_is_connected(self):
        return self._mqtt_is_connected

    @callback
    def _mqtt_connected(self):
        for device in self.mqttdevices.values():
            device.mqtt_connected()
        self._mqtt_is_connected = True

    @callback
    def _mqtt_disconnected(self):
        for device in self.mqttdevices.values():
            device.mqtt_disconnected()
        self._mqtt_is_connected = False

    def get_or_set_discovering(self, device_id: str):
        if device_id not in self.mqttdiscovering:
            self.log(DEBUG, "starting discovery for device_id: %s", device_id)
            # new device discovered: add to discovery state-machine
            self.mqttdiscovering[device_id] = {
                MQTTConnection._KEY_STARTTIME: time(),
                MQTTConnection._KEY_REQUESTTIME: 0,
                MQTTConnection._KEY_REQUESTCOUNT: 0,
            }
            if not self._unsub_discovery_callback:
                self._unsub_discovery_callback = schedule_async_callback(
                    ApiProfile.hass,
                    PARAM_UNAVAILABILITY_TIMEOUT + 2,
                    self._async_discovery_callback,
                )
        return self.mqttdiscovering[device_id]

    async def _async_progress_discovery(self, discovered: dict, device_id: str):
        for namespace in (mc.NS_APPLIANCE_SYSTEM_ALL, mc.NS_APPLIANCE_SYSTEM_ABILITY):
            if get_namespacekey(namespace) not in discovered:
                await self.async_mqtt_publish(
                    device_id,
                    *get_default_arguments(namespace),
                    self.profile.key,
                )
                discovered[MQTTConnection._KEY_REQUESTTIME] = time()
                discovered[MQTTConnection._KEY_REQUESTCOUNT] += 1
                return True

        return False

    async def _async_discovery_callback(self):
        """
        async task to keep alive the discovery process:
        activated when any device is initially detected
        this task is not renewed when the list of devices
        under 'discovery' is empty or these became stale
        """
        self._unsub_discovery_callback = None
        if len(discovering := self.mqttdiscovering) == 0:
            return

        epoch = time()
        for device_id, discovered in discovering.copy().items():
            if not self._mqtt_is_connected:
                break
            if (discovered[MQTTConnection._KEY_REQUESTCOUNT]) > 5:
                # stale entry...remove
                discovering.pop(device_id)
                continue
            if (
                epoch - discovered[MQTTConnection._KEY_REQUESTTIME]
            ) > PARAM_UNAVAILABILITY_TIMEOUT:
                await self._async_progress_discovery(discovered, device_id)

        if len(discovering):
            self._unsub_discovery_callback = schedule_async_callback(
                ApiProfile.hass,
                PARAM_UNAVAILABILITY_TIMEOUT + 2,
                self._async_discovery_callback,
            )


class MerossMQTTConnection(MQTTConnection, MerossMQTTClient):
    profile: MerossCloudProfile  # Type: ignore

    _MSG_PRIORITY_MAP = {
        mc.METHOD_SET: True,
        mc.METHOD_PUSH: False,
        mc.METHOD_GET: None,
    }
    __slots__ = ("_unsub_random_disconnect",)

    def __init__(
        self, profile: MerossCloudProfile, connection_id: str, broker: tuple[str, int]
    ):
        MerossMQTTClient.__init__(self, profile._config, profile.app_id)
        MQTTConnection.__init__(self, profile, connection_id, broker)
        self.user_data_set(ApiProfile.hass)  # speedup hass lookup in callbacks
        self.on_message = self._mqttc_message
        self.on_connect = self._mqttc_connect
        self.on_disconnect = self._mqttc_disconnect

        if MEROSSDEBUG:

            @callback
            def _random_disconnect():
                if self.state_inactive:
                    if MEROSSDEBUG.mqtt_random_connect():
                        self.log(DEBUG, "random connect")
                        self.safe_connect(*self.broker)
                else:
                    if MEROSSDEBUG.mqtt_random_disconnect():
                        self.log(DEBUG, "random disconnect")
                        self.safe_disconnect()
                self._unsub_random_disconnect = schedule_callback(
                    ApiProfile.hass, 60, _random_disconnect
                )

            self._unsub_random_disconnect = schedule_callback(
                ApiProfile.hass, 60, _random_disconnect
            )
        else:
            self._unsub_random_disconnect = None

    async def async_shutdown(self):
        if self._unsub_random_disconnect:
            self._unsub_random_disconnect.cancel()
            self._unsub_random_disconnect = None
        await super().async_shutdown()
        await self.schedule_disconnect()

    def schedule_connect(self):
        # even if safe_connect should be as fast as possible and thread-safe
        # we still might incur some contention with thread stop/restart
        # so we delegate its call to an executor
        return ApiProfile.hass.async_add_executor_job(self.safe_connect, *self.broker)

    def schedule_disconnect(self):
        # same as connect. safe_disconnect should be even faster and less
        # contending but...
        return ApiProfile.hass.async_add_executor_job(self.safe_disconnect)

    def attach(self, device: MerossDevice):
        super().attach(device)
        if self.state_inactive:
            self.schedule_connect()

    def detach(self, device: MerossDevice):
        super().detach(device)
        if not self.mqttdevices:
            self.schedule_disconnect()

    def mqtt_publish(
        self,
        device_id: str,
        namespace: str,
        method: str,
        payload: dict,
        key: KeyType = None,
        messageid: str | None = None,
    ) -> asyncio.Future:
        def _publish():
            if not self.allow_mqtt_publish:
                self.warning(
                    "MQTT publishing is not allowed for this profile (device_id=%s)",
                    device_id,
                    timeout=14400,
                )
                return

            ret = self.rl_publish(
                mc.TOPIC_REQUEST.format(device_id),
                json_dumps(
                    build_payload(
                        namespace,
                        method,
                        payload,
                        key,
                        self.topic_command,
                        messageid,
                    )
                ),
                MerossMQTTConnection._MSG_PRIORITY_MAP[method],
            )
            if ret is False:
                self.warning(
                    "MQTT DROP device_id:(%s) method:(%s) namespace:(%s)",
                    device_id,
                    method,
                    namespace,
                    timeout=14000,
                )
            elif ret is True:
                self.log(
                    DEBUG,
                    "MQTT QUEUE device_id:(%s) method:(%s) namespace:(%s)",
                    device_id,
                    method,
                    namespace,
                )
            else:
                self.log(
                    DEBUG,
                    "MQTT SEND device_id:(%s) method:(%s) namespace:(%s)",
                    device_id,
                    method,
                    namespace,
                )

        return ApiProfile.hass.async_add_executor_job(_publish)

    async def async_mqtt_publish(
        self,
        device_id: str,
        namespace: str,
        method: str,
        payload: dict,
        key: KeyType = None,
        messageid: str | None = None,
    ):
        await self.mqtt_publish(device_id, namespace, method, payload, key, messageid)

    def _mqttc_message(self, client, userdata: HomeAssistant, msg: mqtt.MQTTMessage):
        userdata.create_task(self.async_mqtt_message(msg))

    def _mqttc_connect(self, client, userdata: HomeAssistant, rc, other):
        MerossMQTTClient._mqttc_connect(self, client, userdata, rc, other)
        userdata.add_job(self._mqtt_connected)

    def _mqttc_disconnect(self, client, userdata: HomeAssistant, rc):
        MerossMQTTClient._mqttc_disconnect(self, client, userdata, rc)
        userdata.add_job(self._mqtt_disconnected)


class MerossCloudProfileStoreType(typing.TypedDict):
    appId: str
    token: str | None
    deviceInfo: DeviceInfoDictType
    deviceInfoTime: float


class MerossCloudProfileStore(storage.Store[MerossCloudProfileStoreType]):
    VERSION = 1

    def __init__(self, profile_id: str):
        super().__init__(
            ApiProfile.hass,
            MerossCloudProfileStore.VERSION,
            f"{DOMAIN}.profile.{profile_id}",
        )


class MerossCloudProfile(ApiProfile):
    """
    Represents and manages a cloud account profile used to retrieve keys
    and/or to manage cloud mqtt connection(s)
    """

    KEY_APP_ID: Final = "appId"
    KEY_DEVICE_INFO: Final = "deviceInfo"
    KEY_DEVICE_INFO_TIME: Final = "deviceInfoTime"
    KEY_SUBDEVICE_INFO: Final = "__subDeviceInfo"

    _config: ProfileConfigType
    _data: MerossCloudProfileStoreType
    app_id: str

    __slots__ = (
        "mqttconnections",
        "linkeddevices",
        "_config",
        "_data",
        "app_id",
        "_store",
        "_unsub_polling_query_devices",
    )

    def __init__(self, config_entry: ConfigEntry):
        _config: ProfileConfigType = config_entry.data  # type: ignore
        super().__init__(_config[mc.KEY_USERID_], config_entry)
        self.mqttconnections: dict[str, MerossMQTTConnection] = {}
        self.linkeddevices: dict[str, MerossDevice] = {}
        self._config = _config
        self._store = MerossCloudProfileStore(self.id)
        self._unsub_polling_query_devices: asyncio.TimerHandle | None = None

    async def async_start(self):
        """
        Performs 'cold' initialization of the profile by checking
        if we need to update the device_info and eventually start the
        unknown devices discovery.
        We'll eventually setup the mqtt listeners in case our
        configured devices don't match the profile list. This usually means
        the user has binded a new device and we need to 'discover' it.
        """
        if data := await self._store.async_load():
            self._data = data
            self.app_id = data.get(MerossCloudProfile.KEY_APP_ID)
            if not (self.app_id and isinstance(self.app_id, str)):
                data[MerossCloudProfile.KEY_APP_ID] = self.app_id = generate_app_id()
            if not isinstance(data.get(MerossCloudProfile.KEY_DEVICE_INFO), dict):
                data[MerossCloudProfile.KEY_DEVICE_INFO] = {}
            self._last_query_devices = data.get(
                MerossCloudProfile.KEY_DEVICE_INFO_TIME, 0.0
            )
            if not isinstance(self._last_query_devices, float):
                data[
                    MerossCloudProfile.KEY_DEVICE_INFO_TIME
                ] = self._last_query_devices = 0.0
        else:
            self.app_id = generate_app_id()
            self._last_query_devices = 0.0
            data: MerossCloudProfileStoreType | None = {
                MerossCloudProfile.KEY_APP_ID: self.app_id,
                mc.KEY_TOKEN: self._config[mc.KEY_TOKEN],
                MerossCloudProfile.KEY_DEVICE_INFO: {},
                MerossCloudProfile.KEY_DEVICE_INFO_TIME: 0.0,
            }
            self._data = data

        # compute the next cloud devlist query and setup the scheduled callback
        next_query_epoch = (
            self._last_query_devices + PARAM_CLOUDPROFILE_QUERY_DEVICELIST_TIMEOUT
        )
        next_query_delay = next_query_epoch - time()
        if next_query_delay < 60:
            # schedule immediately when it's about to come
            # or if the timer elapsed in the past
            if await self.async_query_devices() is not None:
                # the 'unknown' devices discovery already kicked in
                # when the "async_query_devices" processed data
                return
            next_query_delay = 60
        # the device_info refresh did not kick in or failed
        # for whatever reason. We just scan the device_info
        # we have and setup the polling
        device_info_unknown = [
            device_info
            for device_id, device_info in data[
                MerossCloudProfile.KEY_DEVICE_INFO
            ].items()
            if device_id not in ApiProfile.devices
        ]
        if len(device_info_unknown):
            await self._process_device_info_unknown(device_info_unknown)

        """REMOVE
        with self._cloud_token_exception_manager("async_cloudapi_deviceinfo") as token:
            if token is not None:
                for device_id, device_info in self[self.KEY_DEVICE_INFO].items():
                    _data = await async_cloudapi_device_devextrainfo(
                        token, device_id, async_get_clientsession(ApiProfile.hass)
                    )
                    self.log(
                        DEBUG,
                        "Device/devExtraInfo(%s): %s",
                        device_id,
                        json_dumps(_data),
                    )
        """
        assert self._unsub_polling_query_devices is None
        self._unsub_polling_query_devices = schedule_async_callback(
            ApiProfile.hass,
            next_query_delay,
            self._async_polling_query_devices,
        )

    async def async_shutdown(self):
        ApiProfile.profiles[self.id] = None
        for mqttconnection in self.mqttconnections.values():
            await mqttconnection.async_shutdown()
        self.mqttconnections.clear()
        for device in self.linkeddevices.values():
            device.profile_unlinked()
        self.linkeddevices.clear()
        if self._unsub_polling_query_devices:
            self._unsub_polling_query_devices.cancel()
            self._unsub_polling_query_devices = None
        await super().async_shutdown()

    # interface: EntityManager
    async def entry_update_listener(self, hass, config_entry: ConfigEntry):
        config: ProfileConfigType = config_entry.data  # type: ignore
        allow_mqtt_publish = config.get(CONF_ALLOW_MQTT_PUBLISH)
        if allow_mqtt_publish != self.allow_mqtt_publish:
            # device._mqtt_publish is rather 'passive' so
            # we do some fast 'smart' updates:
            if allow_mqtt_publish:
                for device in self.linkeddevices.values():
                    device._mqtt_publish = device._mqtt_connected
            else:
                for device in self.linkeddevices.values():
                    device._mqtt_publish = None
        self._config = config
        await self.async_update_credentials(config)

    # interface: ApiProfile
    @property
    def allow_mqtt_publish(self):
        return self._config.get(CONF_ALLOW_MQTT_PUBLISH)

    def attach_mqtt(self, device: MerossDevice):
        if device.id not in self._data[MerossCloudProfile.KEY_DEVICE_INFO]:
            self.warning(
                "cannot connect MQTT for MerossDevice(%s): it does not belong to the current profile",
                device.name,
            )
            return

        with self.exception_warning("attach_mqtt"):
            self._get_or_create_mqttconnection(device.mqtt_broker).attach(device)

    # interface: self
    @property
    def create_diagnostic_entities(self):
        return self._config.get(CONF_CREATE_DIAGNOSTIC_ENTITIES)

    @property
    def token(self):
        return self._data.get(mc.KEY_TOKEN)

    def link(self, device: MerossDevice):
        device_id = device.id
        if device_id not in self.linkeddevices:
            device_info = self._data[MerossCloudProfile.KEY_DEVICE_INFO].get(device_id)
            if not device_info:
                self.warning(
                    "cannot link MerossDevice(%s): does not belong to the current profile",
                    device.name,
                )
                return
            device.profile_linked(self)
            self.linkeddevices[device_id] = device
            device.update_device_info(device_info)

    def unlink(self, device: MerossDevice):
        device_id = device.id
        if device_id in self.linkeddevices:
            device.profile_unlinked()
            self.linkeddevices.pop(device_id)

    def get_device_info(self, device_id: str) -> DeviceInfoType | None:
        return self._data[MerossCloudProfile.KEY_DEVICE_INFO].get(device_id)

    async def async_update_credentials(self, credentials: MerossCloudCredentials):
        with self.exception_warning("async_update_credentials"):
            assert self.id == credentials[mc.KEY_USERID_]
            assert self.key == credentials[mc.KEY_KEY]
            token = self._data.get(mc.KEY_TOKEN)
            if token != credentials[mc.KEY_TOKEN]:
                self.log(DEBUG, "updating credentials with new token")
                if token:
                    # discard old one to play it nice but token might be expired
                    with self.exception_warning("async_cloudapi_logout"):
                        await async_cloudapi_logout(
                            token, async_get_clientsession(ApiProfile.hass)
                        )
                self._data[mc.KEY_TOKEN] = credentials[mc.KEY_TOKEN]
                self._schedule_save_store()
                # the 'async_check_query_devices' will only occur if we didn't refresh
                # on our polling schedule for whatever reason (invalid token -
                # no connection - whatsoever) so, having a fresh token and likely
                # good connectivity we're going to retrigger that
                await self.async_check_query_devices()

    async def async_query_devices(self):
        with self._cloud_token_exception_manager("async_query_devices") as token:
            self.log(
                DEBUG,
                "querying device list - last query was at: %s",
                datetime_from_epoch(self._last_query_devices).isoformat(),
            )
            if not token:
                self.warning("querying device list cancelled: missing api token")
                return None
            self._last_query_devices = time()
            device_info_new = await async_cloudapi_device_devlist(
                token, async_get_clientsession(ApiProfile.hass)
            )
            await self._process_device_info_new(device_info_new)
            self._data[self.KEY_DEVICE_INFO_TIME] = self._last_query_devices
            self._schedule_save_store()
            # retrigger the poll at the right time since async_query_devices
            # might be called for whatever reason 'asynchronously'
            # at any time (say the user does a new cloud login or so...)
            if self._unsub_polling_query_devices:
                self._unsub_polling_query_devices.cancel()
            self._unsub_polling_query_devices = schedule_async_callback(
                ApiProfile.hass,
                PARAM_CLOUDPROFILE_QUERY_DEVICELIST_TIMEOUT,
                self._async_polling_query_devices,
            )
            return device_info_new

        return None

    def need_query_devices(self):
        return (
            time() - self._last_query_devices
        ) > PARAM_CLOUDPROFILE_QUERY_DEVICELIST_TIMEOUT

    async def async_check_query_devices(self):
        if self.need_query_devices():
            return await self.async_query_devices()
        return None

    def _get_or_create_mqttconnection(self, broker: tuple[str, int]):
        connection_id = f"{self.id}:{broker[0]}:{broker[1]}"
        if connection_id not in self.mqttconnections:
            self.mqttconnections[connection_id] = MerossMQTTConnection(
                self, connection_id, broker
            )
        return self.mqttconnections[connection_id]

    @contextmanager
    def _cloud_token_exception_manager(self, msg: str, *args, **kwargs):
        try:
            yield self._data.get(mc.KEY_TOKEN)
        except CloudApiError as clouderror:
            if clouderror.apistatus in APISTATUS_TOKEN_ERRORS:
                self._data.pop(mc.KEY_TOKEN, None)  # type: ignore
            self.log_exception_warning(clouderror, msg)
        except Exception as exception:
            self.log_exception_warning(exception, msg)

    async def _async_polling_query_devices(self):
        try:
            self._unsub_polling_query_devices = None
            await self.async_query_devices()
        finally:
            if self._unsub_polling_query_devices is None:
                # this happens when 'async_query_devices' is unable to
                # retrieve fresh cloud data for whatever reason
                self._unsub_polling_query_devices = schedule_async_callback(
                    ApiProfile.hass,
                    PARAM_CLOUDPROFILE_QUERY_DEVICELIST_TIMEOUT,
                    self._async_polling_query_devices,
                )

    async def _async_query_subdevices(self, device_id: str):
        with self._cloud_token_exception_manager("_async_query_subdevices") as token:
            if not token:
                self.warning("querying subdevice list cancelled: missing api token")
                return None
            self.log(DEBUG, "querying subdevice list")
            return await async_cloudapi_hub_getsubdevices(
                token, device_id, async_get_clientsession(ApiProfile.hass)
            )
        return None

    async def _process_device_info_new(
        self, device_info_list_new: list[DeviceInfoType]
    ):
        device_info_dict = self._data[MerossCloudProfile.KEY_DEVICE_INFO]
        device_info_removed = {device_id for device_id in device_info_dict.keys()}
        device_info_unknown: list[DeviceInfoType] = []
        for device_info in device_info_list_new:
            with self.exception_warning("_process_device_info_new"):
                device_id = device_info[mc.KEY_UUID]
                # preserved (old) dict of hub subdevices to process/carry over
                # for MerossDeviceHub(s)
                sub_device_info_dict: dict[str, SubDeviceInfoType] | None
                if device_id in device_info_dict:
                    # already known device
                    device_info_removed.remove(device_id)
                    sub_device_info_dict = device_info_dict[device_id].get(
                        MerossCloudProfile.KEY_SUBDEVICE_INFO
                    )
                else:
                    # new device
                    sub_device_info_dict = None
                device_info_dict[device_id] = device_info

                if device_id not in ApiProfile.devices:
                    device_info_unknown.append(device_info)
                    continue

                if (device := ApiProfile.devices[device_id]) is None:
                    # config_entry for device is not loaded
                    continue

                if isinstance(device, MerossDeviceHub):
                    if sub_device_info_dict is None:
                        sub_device_info_dict = {}
                    device_info[
                        MerossCloudProfile.KEY_SUBDEVICE_INFO
                    ] = sub_device_info_dict
                    sub_device_info_list_new = await self._async_query_subdevices(
                        device_id
                    )
                    if sub_device_info_list_new is not None:
                        await self._process_subdevice_info_new(
                            device, sub_device_info_dict, sub_device_info_list_new
                        )
                device.update_device_info(device_info)

        for device_id in device_info_removed:
            device_info_dict.pop(device_id)
            # TODO: warn the user? should we remove the device ?

        if len(device_info_unknown):
            await self._process_device_info_unknown(device_info_unknown)

    async def _process_subdevice_info_new(
        self,
        hub_device: MerossDeviceHub,
        sub_device_info_dict: dict[str, SubDeviceInfoType],
        sub_device_info_list_new: list[SubDeviceInfoType],
    ):
        sub_device_info_removed = {
            subdeviceid for subdeviceid in sub_device_info_dict.keys()
        }
        sub_device_info_unknown: list[SubDeviceInfoType] = []

        for sub_device_info in sub_device_info_list_new:
            with self.exception_warning("_process_subdevice_info_new"):
                subdeviceid = sub_device_info[mc.KEY_SUBDEVICEID]
                if subdeviceid in sub_device_info_dict:
                    # already known device
                    sub_device_info_removed.remove(subdeviceid)

                sub_device_info_dict[subdeviceid] = sub_device_info
                if subdevice := hub_device.subdevices.get(subdeviceid):
                    subdevice.update_device_info(sub_device_info)
                else:
                    sub_device_info_unknown.append(sub_device_info)

        for subdeviceid in sub_device_info_removed:
            sub_device_info_dict.pop(subdeviceid)
            # TODO: warn the user? should we remove the subdevice from the hub?

        if len(sub_device_info_unknown):
            # subdevices were added.. discovery should be managed by the hub itself
            # TODO: warn the user ?
            pass

    async def _process_device_info_unknown(
        self, device_info_unknown: list[DeviceInfoType]
    ):
        if not self.allow_mqtt_publish:
            self.warning(
                "Meross cloud api reported new devices but MQTT publishing is disabled: skipping automatic discovery",
                timeout=604800,  # 1 week
            )
            return

        config_entries_helper = ConfigEntriesHelper(ApiProfile.hass)
        for device_info in device_info_unknown:
            with self.exception_warning("_process_device_info_unknown"):
                device_id = device_info[mc.KEY_UUID]
                if config_entries_helper.get_config_flow(device_id):
                    continue
                # cloud conf has a new device
                for hostkey in (mc.KEY_DOMAIN, mc.KEY_RESERVEDDOMAIN):
                    with self.exception_warning(
                        f"_process_device_info_unknown: unknown device_id={device_id}"
                    ):
                        broker = parse_domain(domain := device_info[hostkey])
                        mqttconnection = self._get_or_create_mqttconnection(broker)
                        if mqttconnection.state_inactive:
                            await mqttconnection.schedule_connect()
                        mqttconnection.get_or_set_discovering(device_id)
                        if domain == device_info[mc.KEY_RESERVEDDOMAIN]:
                            # dirty trick to avoid looping when the 2 hosts
                            # are the same
                            break

    def _schedule_save_store(self):
        def _data_func():
            return self._data

        self._store.async_delay_save(
            _data_func, PARAM_CLOUDPROFILE_DELAYED_SAVE_TIMEOUT
        )
