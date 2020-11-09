import pyudev
import logging
from time import sleep
import shutil
from typing import Union
import os
from queue import Queue, Empty
from threading import Thread, Lock
from pathlib import Path
from contextlib import suppress

dev_queue = Queue(maxsize=10)
POLL_SLEEP_TIME = 5 #seconds

def check_action(action, device):
    if action in ('add', 'remove'):
        dev_queue.put((action, device))

def convert_bytes(size: Union[str,int,float]):
    '''
        Convert a byte representation to Human Readable format
        
        Args:
            size (str/int/float): Size in bytes e.g. 1024
         
        Returns:
            str: Human Readable size e.g. "1KB"
    '''
    if isinstance(size, str):
        size = float(size)
    for x in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024.0:
            return "%3.2f %s" % (size, x)
        size /= 1024.0
    return str(size)


class USBMonitor:
    def __init__(self, only_removable=True):
        self._timeout_in_sec = 1 #ms
        self._destruct = False
        self._only_removable = only_removable
        self._async_mode = None
        self._from_glib = True

    def _create_monitor(self):
        self._context = pyudev.Context()
        self._monitor = pyudev.Monitor.from_netlink(self._context)
        self._monitor.filter_by('block', device_type="partition")

    def add_pre_ejected_devices(self):
        for device in self._context.list_devices(subsystem="block", DEVTYPE="partition"):
            if device.properties.get('ID_BUS')=="usb":
                check_action('add', device)

    def start_polling(self):
        self._async_mode = False
        self._create_monitor()
        self.add_pre_ejected_devices()
        while not self._destruct:
            try:
                dev = None
                dev = self._monitor.poll(timeout=self._timeout_in_sec)
            except (KeyboardInterrupt, StopIteration):
                self.destroy()
            else:
                if dev and dev.properties.get('ID_FS_TYPE'):
                    if self._only_removable and not dev.properties.get('ID_BUS')=="usb":
                        continue
                    self._show_notification(dev.action, dev)
                    check_action(dev.action, dev)
    
    def start_asynchronous_polling(self):
        self._async_mode = True
        self._create_monitor()
        self.add_pre_ejected_devices()
        try:
            from pyudev.glib import MonitorObserver

            logging.debug("Monitor running from pyudev.glib\n")
            def log_event(observer, device):
                if device and device.properties.get('ID_FS_TYPE'):
                    if self._only_removable and not device.properties.get('ID_BUS')=="usb":
                        return
                    self._show_notification(device.action, device)
                    check_action(device.action, device)
        except:
            try:
                from pyudev.glib import GUDevMonitorObserver as MonitorObserver

                logging.debug("Monitor running from pyudev.glib GUDevMonitor\n")
                def log_event(observer, action, device):
                    if device and device.properties.get('ID_FS_TYPE'):
                        if self._only_removable and not device.properties.get('ID_BUS')=="usb":
                            return
                        self._show_notification(action, device)
                        check_action(action, device)
            except:
                from pyudev import MonitorObserver

                logging.debug("Monitor running from pyudev\n")
                def log_event(action, device):
                    if device and device.properties.get('ID_FS_TYPE'):
                        if self._only_removable and not device.properties.get('ID_BUS')=="usb":
                            return
                    self._show_notification(action, device)
                    check_action(action, device)

                self._from_glib = False
        if self._from_glib:
            logging.debug('Glib activated\n')
            self._async_monitor = MonitorObserver(self._monitor)
            self._async_monitor.deviceEvent.connect(log_event)
            self._monitor.start()
        else:
            self._async_monitor = MonitorObserver(self._monitor, log_event)
            self._async_monitor.start()

    def _show_notification(self, action, dev):
        logging.info(f'''Action: {dev.action}
            ID_FS_LABEL: {dev.properties.get('ID_FS_LABEL')}
            ID_VENDOR: {dev.properties.get('ID_VENDOR')}
            ID_MODEL: {dev.properties.get('ID_MODEL')}
            ID_TYPE: {dev.properties.get('ID_TYPE')}
            ID_BUS: {dev.properties.get('ID_BUS')}
            ID_FS_VERSION: {dev.properties.get('ID_FS_VERSION')}
        ''')

    def destroy(self):
        self._destruct = True
        if self._async_mode:
            self._async_monitor.stop()


class USBDrive:
    def __init__(self, device):
        self._device = device
        self.storage_info = None
        self.prev_storage_info = None
        logging.debug(f"Device Node: '{self._device.device_node}'")
        self.ID_FS_LABEL = self._device.properties.get('ID_FS_LABEL')
        self.ID_VENDOR = self._device.properties.get('ID_VENDOR')
        self.ID_MODEL = self._device.properties.get('ID_MODEL')
        self.get_device_mount_point()
        self.get_storage_information()

    def get_device_mount_point(self):
        self._mount_point = None
        with open('/proc/mounts','r') as f:
            for l in f.readlines():
                if l.startswith(self._device.device_node):
                    parts = l.split(' ')
                    location = parts[1].replace('\\040',' ')
                    logging.debug(f"Part 1: '{location}'")
                    logging.debug(f'Type of location: {type(location)}')
                    if os.path.isdir(location) and os.path.ismount(location):
                        self._mount_point = Path(location)
                        logging.debug(f'Mount point: {self._mount_point}')
                        return
        logging.error('''USB Drive mount point not found''')

    def get_storage_information(self):
        if self._mount_point is None or not self._mount_point.exists():
            # logging.debug(f'Mount Point: {self._mount_point}')
            # logging.debug('Cannot find storage information')
            return None
        # else:
        #     with suppress(AttributeError):
        #         logging.debug(f'Exists: {self._mount_point.exists()}')
        self.prev_storage_info = self.storage_info
        self.storage_info = shutil.disk_usage(self._mount_point)
        return self.storage_info

    def get_free_space_changes(self, notify_changes=False):
        if self.prev_storage_info is None or self.storage_info is None:
            return 0
        change_in_freespace = self.prev_storage_info.free - self.storage_info.free
        # logging.debug(f'Free space query: {change_in_freespace}')
        if notify_changes and change_in_freespace:
            self.notify_change(change_in_freespace)
        return change_in_freespace

    def notify_change(self, size):
        text = 'Size Added'
        if size < 0:
            text = 'Size Removed'
            size = -size
        logging.info(f'''Drive Modification
            ID_FS_LABEL: {self.ID_FS_LABEL}
            ID_VENDOR: {self.ID_VENDOR}
            ID_MODEL: {self.ID_MODEL}
            {text}: {convert_bytes(size)}
        ''')


class Poller(Thread):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.device_avail = {}
        self._destruct = False

    def create_device(self, device):
        if device.device_node in self.device_avail:
            self.remove_device(device)
        self.device_avail.update({device.device_node: USBDrive(device)})

    def remove_device(self, device):
        dev_data = self.device_avail.get(device.device_node)
        if dev_data:
            dev = self.device_avail.pop(device.device_node)
            del dev

    def run(self):
        while not self._destruct:
            try:
                while dev_queue.qsize() > 0:
                    try:
                        action, device = dev_queue.get(timeout=POLL_SLEEP_TIME)
                        if action == 'add':
                            logging.debug('Adding Device')
                            self.create_device(device)
                        elif action == 'remove':
                            logging.debug('Removing Device')
                            self.remove_device(device)
                    except Empty:
                        break
                for node,usb in self.device_avail.items():
                    try:
                        _ = usb.get_storage_information()
                        usb.get_free_space_changes(notify_changes=True)
                    except AttributeError:
                        logging.error('Attribute error for a device. Deleting from dict')
                        self.remove_device(usb._device)
                sleep(POLL_SLEEP_TIME)
            except (KeyboardInterrupt, StopIteration):
                break

    def destroy(self):
        self._destruct = True


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

    monitor = USBMonitor()
    poller = Poller()

    logging.debug('''Starting Monitor
        Press Ctrl+C to quit
    ''')
    #monitor.start_polling() #synchronous method

    poller.start()
    monitor.start_asynchronous_polling()
    while True:
        try:
            q = input("")
            if q in ('q','Q'):
                break
        except (KeyboardInterrupt, StopIteration):
            break
    
    logging.debug(f'''Stopping Monitor and poller. Wait upto {POLL_SLEEP_TIME} sec''')

    monitor.destroy()
    poller.destroy()
    poller.join()

    