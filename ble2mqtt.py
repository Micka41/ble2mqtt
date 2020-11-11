import asyncio as aio
import json
import logging
import os
import typing as ty

import aio_mqtt
from bleak import BleakError

from devices import registered_device_types
from devices.base import Device

# linux only but don't crash on other systems
try:
    from txdbus.error import RemoteError  # noqa
except ImportError:
    RemoteError = BleakError


logger = logging.getLogger(__name__)

VERSION = '0.1.0a0'

CONFIG_MQTT_NAMESPACE = 'homeassistant'


ListOfConnectionErrors = (
    ConnectionError,
    BleakError,
    RemoteError,
    aio.TimeoutError,
    # AttributeError: 'NoneType' object has no attribute 'callRemote'
    AttributeError,
)


class Ble2Mqtt:
    TOPIC_ROOT = 'ble2mqtt'
    BRIDGE_TOPIC = 'bridge'

    def __init__(
            self,
            host: str,
            port: int = None,
            user: ty.Optional[str] = None,
            password: ty.Optional[str] = None,
            reconnection_interval: int = 10,
            loop: ty.Optional[aio.AbstractEventLoop] = None,
    ) -> None:
        self._mqtt_host = host
        self._mqtt_port = port
        self._mqtt_user = user
        self._mqtt_password = password

        self._reconnection_interval = reconnection_interval
        self._loop = loop or aio.get_event_loop()
        self._client = aio_mqtt.Client(loop=self._loop)
        self._root_tasks = []

        self._device_manage_tasks = {}

        self.availability_topic = '/'.join((
            self.TOPIC_ROOT,
            self.BRIDGE_TOPIC,
            'state',
        ))

        self.device_registry: ty.List[Device] = []

    def start(self):
        self._root_tasks = [
            self._loop.create_task(self._connect_forever()),
            self._loop.create_task(self._handle_messages()),
        ]

    @staticmethod
    async def stop_task(task):
        logger.debug(f'stop_task {task=}')
        task.cancel()
        try:
            await task
        except aio.CancelledError:
            logger.debug(f'{task} is now cancelled')

    async def close(self) -> None:
        for task in self._root_tasks:
            await self.stop_task(task)
        for k, task in self._device_manage_tasks.items():
            await self.stop_task(task)
        if self._client.is_connected():
            await self._client.disconnect()

    def _get_topic(self, dev_id, subtopic):
        return '/'.join((self.TOPIC_ROOT, dev_id, subtopic))

    def register(self, device: Device):
        if not device:
            return
        self.device_registry.append(device)

    @property
    def subscribed_topics(self):
        return [
            '/'.join((self.TOPIC_ROOT, topic))
            for device in self.device_registry
            for topic in device.subscribed_topics
        ]

    async def publish_topic_callback(self, topic, value):
        logger.debug(f'call publish callback {topic=} {value=}')
        await self._client.publish(
            aio_mqtt.PublishableMessage(
                topic_name='/'.join((self.TOPIC_ROOT, topic)),
                payload=value,
                qos=aio_mqtt.QOSLevel.QOS_1,
            ),
        )

    async def _handle_messages(self) -> None:
        async for message in self._client.delivered_messages(
            f'{self.TOPIC_ROOT}/#',
        ):
            logger.debug(message)
            while True:
                if message.topic_name not in self.subscribed_topics:
                    continue

                topic_wo_prefix = message.topic_name.removeprefix(
                    f'{self.TOPIC_ROOT}/',
                )
                for _device in self.device_registry:
                    if topic_wo_prefix in _device.subscribed_topics:
                        device = _device
                        break
                else:
                    raise NotImplementedError('Unknown topic')
                if not await device.client.is_connected():
                    logger.warning(
                        f'Received topic {topic_wo_prefix} '
                        f'with {message.payload} '
                        f' but {device.client} is offline',
                    )
                    continue

                try:
                    value = json.loads(message.payload)
                except ValueError:
                    value = message.payload.decode()

                await device.add_incoming_message(topic_wo_prefix, value)
                break

            await aio.sleep(1)

    async def send_device_config(self, device: Device):
        device_info = {
            'identifiers': [
                device.unique_id,
            ],
            'name': device.unique_id,
            'sw_version': device.version,
            'model': device.model,
            'manufacturer': device.manufacturer,
        }

        def get_generic_vals(entity: dict):
            name = entity.pop('name')
            result = {
                'name': f'{name}_{device.dev_id}',
                'unique_id': f'{name}_{device.dev_id}',
                'device': device_info,
            }
            icon = entity.pop('icon', None)
            if icon:
                result['icon'] = f'mdi:{icon}'
            result.update(entity)
            return result

        for cls, entities in device.entities.items():
            if cls == 'switch':
                for entity in entities:
                    entity_name = entity['name']
                    state_topic = self._get_topic(device.unique_id, entity_name)
                    command_topic = '/'.join((state_topic, device.SET_POSTFIX))
                    config_topic = '/'.join((
                        CONFIG_MQTT_NAMESPACE,
                        cls,
                        device.dev_id,
                        entity_name,
                        'config',
                    ))
                    payload = json.dumps({
                        **get_generic_vals(entity),
                        'state_topic': state_topic,
                        'command_topic': command_topic,
                    })
                    logger.debug(
                        f'Publish config {config_topic=}: {payload=}',
                    )
                    await self._client.publish(
                        aio_mqtt.PublishableMessage(
                            topic_name=config_topic,
                            payload=payload,
                            qos=aio_mqtt.QOSLevel.QOS_1,
                            retain=True,
                        ),
                    )
                    # TODO: send real state on receiving status from a device
                    logger.debug(f'Publish initial state {state_topic=}')
                    await self._client.publish(
                        aio_mqtt.PublishableMessage(
                            topic_name=state_topic,
                            payload='OFF',
                            qos=aio_mqtt.QOSLevel.QOS_1,
                        ),
                    )
            if cls == 'sensor':
                for entity in entities:
                    entity_name = entity['name']
                    state_topic = self._get_topic(device.unique_id, 'state')
                    config_topic = '/'.join((
                        CONFIG_MQTT_NAMESPACE,
                        cls,
                        device.dev_id,
                        entity_name,
                        'config',
                    ))
                    payload = json.dumps({
                        **get_generic_vals(entity),
                        'state_topic': state_topic,
                        'value_template': f'{{{{ value_json.{entity_name} }}}}',
                    })
                    logger.debug(
                        f'Publish config {config_topic=}: {payload=}',
                    )
                    await self._client.publish(
                        aio_mqtt.PublishableMessage(
                            topic_name=config_topic,
                            payload=payload,
                            qos=aio_mqtt.QOSLevel.QOS_1,
                            retain=True,
                        ),
                    )

    async def manage_device(self, device: Device):
        logger.debug(f'Start managing {device=}')
        while True:
            logger.debug(f'Connecting to {device=}')
            connect_task = self._loop.create_task(device.connect())
            finished, unfinished = await aio.wait(
                [connect_task],
                timeout=20,
            )
            try:
                if connect_task not in finished:
                    logger.debug(f'Stop task {connect_task=} {device=}')
                    # connect_task.cancel()
                    await self.stop_task(connect_task)
                    raise ConnectionError(f'Task is timed out {device=}')
                else:
                    t, = finished
                    disconnect_fut = t.result()
            except ListOfConnectionErrors as e:
                logger.warning(f'Error while connecting to {device=}, {e}')
                await device.close()
                continue

            try:
                # retrieve version and details
                logger.debug(f'get_device_data {device=}')
                await device.get_device_data()
            except ListOfConnectionErrors:
                logger.exception(f'Cannot get initial info {device=}')
                await device.close()
                continue

            try:
                await self.send_device_config(device)

                if device.subscribed_topics:
                    await self._client.subscribe(*[
                        (
                            '/'.join((self.TOPIC_ROOT, topic)),
                            aio_mqtt.QOSLevel.QOS_1,
                        )
                        for topic in device.subscribed_topics
                    ])
            except aio_mqtt.Error:
                logger.exception(f'Cannot subscribe to topics {device=}')
                await device.client.disconnect()
                continue

            try:
                logger.info(
                    f'Start device {device=} handle task and wait '
                    f'for disconnect',
                )
                finished, unfinished = await aio.wait(
                    [
                        disconnect_fut,
                        self._loop.create_task(
                            device.handle(self.publish_topic_callback),
                        ),
                        self._loop.create_task(
                            device.handle_messages(self.publish_topic_callback),
                        ),
                    ],
                    return_when=aio.FIRST_COMPLETED,
                )
                logger.debug(
                    f'Handle tasks finished. {device=} disconnected. '
                    f'{finished=} {unfinished}',
                )
                for t in finished:
                    logger.debug(f'Fetching result {device=} {t=}')
                    t.result()
                for t in unfinished:
                    t.cancel()
                logger.debug(f'wait for cancelling tasks for {device=}')
                await aio.wait(unfinished)
            except Exception:
                logger.exception(f'{device=} raised an error')
            finally:
                await device.close()
                logger.debug(f'unsubscribe from topics for {device=}')
                try:
                    if device.subscribed_topics:
                        await self._client.unsubscribe(*[
                            '/'.join((self.TOPIC_ROOT, topic))
                            for topic in device.subscribed_topics
                        ])
                except aio_mqtt.ConnectionClosedError:
                    logger.exception(
                        'Stop manage task on MQTT connection error',
                    )
                    return

                except Exception as e:
                    logger.exception(
                        f'Couldn\'t stop all tasks for {device=} {e}',
                    )
            logger.info(
                f'Sleep for {device.RECONNECTION_TIMEOUT} secs to '
                f'reconnect to {device=}',
            )
            await aio.sleep(device.RECONNECTION_TIMEOUT)

    async def create_device_manage_tasks(self):
        tasks = []
        for dev in self.device_registry:
            task = self._loop.create_task(self.manage_device(dev))
            self._device_manage_tasks[dev] = task
            tasks.append(task)
        return tasks

    async def stop_device_manage_tasks(self):
        for dev in list(self._device_manage_tasks.keys()):
            logger.info(f'Stopping manage task for {dev=}')
            task = self._device_manage_tasks.pop(dev)
            task.cancel()
            await aio.wait([task])

    async def _connect_forever(self) -> None:
        while True:
            try:
                connect_result = await self._client.connect(
                    host=self._mqtt_host,
                    port=self._mqtt_port,
                    username=self._mqtt_user,
                    password=self._mqtt_password,
                    will_message=aio_mqtt.PublishableMessage(
                        topic_name=self.availability_topic,
                        payload='offline',
                        qos=aio_mqtt.QOSLevel.QOS_1,
                        retain=True,
                    ),
                )
                logger.info(f'Connected to {self._mqtt_host}')
                await self._client.publish(
                    aio_mqtt.PublishableMessage(
                        topic_name=self.availability_topic,
                        payload='online',
                        qos=aio_mqtt.QOSLevel.QOS_1,
                        retain=True,
                    ),
                )
                tasks = await self.create_device_manage_tasks()
                logger.debug("Wait for network interruptions...")
                finished, unfinished = await aio.wait(
                    [
                        connect_result.disconnect_reason,
                        *tasks,
                    ],
                    return_when=aio.FIRST_COMPLETED,
                )
                for t in finished:
                    try:
                        t.result()
                    except Exception:
                        logger.exception('Root task has raised an exception')
                for t in unfinished:
                    t.cancel()
                try:
                    await aio.wait(unfinished)
                except aio.CancelledError:
                    pass
            except aio.CancelledError:
                raise

            except aio_mqtt.AccessRefusedError as e:
                await self.stop_device_manage_tasks()
                logger.error("Access refused", exc_info=e)

            except (
                    aio_mqtt.ConnectionLostError,
                    aio_mqtt.ConnectionClosedError,
                    aio_mqtt.ServerDiedError,
            ) as e:
                try:
                    await self.stop_device_manage_tasks()
                except Exception as e:
                    logger.exception(e)
                logger.error(
                    "Connection lost. Will retry in %d seconds",
                    self._reconnection_interval,
                    exc_info=e,
                )
                await aio.sleep(self._reconnection_interval)

            except aio_mqtt.ConnectionCloseForcedError as e:
                logger.error("Connection close forced", exc_info=e)
                return

            except Exception as e:
                logger.error(
                    "Unhandled exception during connecting",
                    exc_info=e,
                )
                try:
                    await self._client.publish(
                        aio_mqtt.PublishableMessage(
                            topic_name=self.availability_topic,
                            payload='offline',
                            qos=aio_mqtt.QOSLevel.QOS_1,
                            retain=True,
                        ),
                    )
                except Exception:
                    pass
                return
            else:
                try:
                    await self._client.publish(
                        aio_mqtt.PublishableMessage(
                            topic_name=self.availability_topic,
                            payload='offline',
                            qos=aio_mqtt.QOSLevel.QOS_1,
                            retain=True,
                        ),
                    )
                except Exception:
                    pass
                logger.info("Disconnected")
                return


if __name__ == '__main__':
    logging.basicConfig(level='INFO')
    loop = aio.get_event_loop()

    os.environ.setdefault('BLE2MQTT_CONFIG', '/etc/ble2mqtt.json')
    config = {}
    if os.path.exists(os.environ['BLE2MQTT_CONFIG']):
        try:
            with open(os.environ['BLE2MQTT_CONFIG'], 'r') as f:
                config = json.load(f)
        except Exception:
            pass

    config = {
        'mqtt_host': 'localhost',
        'mqtt_port': 1883,
        **config,
    }

    server = Ble2Mqtt(
        reconnection_interval=10,
        loop=loop,
        host=config['mqtt_host'],
        port=config['mqtt_port'],
        user=config.get('mqtt_user'),
        password=config.get('mqtt_password'),
    )

    devices = config.get('devices') or []
    for device in devices:
        try:
            mac = device.pop('address')
            typ = device.pop('type')
        except (ValueError, IndexError):
            continue
        klass = registered_device_types[typ]
        server.register(klass(
            mac=mac,
            loop=loop,
            **device,
        ))

    server.start()

    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass

    finally:
        loop.run_until_complete(server.close())
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
