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

dev_queue: Queue = Queue(maxsize=10)
POLL_SLEEP_TIME: int = 5 #seconds

def check_action(action:str, device:pyudev.Device):
    '''
        Adds the device to queue for Poller to track on.

        Args:
            action (str): Can be any of the action by watcher add/remove/change/move
            device (Device): An instance of pyudev Device      
    '''
    if action in ('add', 'remove'):
        logging.debug(f'{action.capitalize()}ing Device to queue')
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
    '''
        Monitor class that keeps track of USB add and remove
    '''
    def __init__(self, only_removable:bool=True):
        self._timeout_in_sec = 1 #ms
        self._destruct = False
        self._only_removable = only_removable
        self._async_mode = None
        self._from_glib = True

    def _create_monitor(self):
        '''
            Creates context and monitor objects
        '''
        self._context = pyudev.Context()
        self._monitor = pyudev.Monitor.from_netlink(self._context)
        self._monitor.filter_by('block', device_type="partition")

    def add_pre_ejected_devices(self):
        '''
            If any device is already in the USB before the application starts. It adds those devices to queue.
        '''
        for device in self._context.list_devices(subsystem="block", DEVTYPE="partition"):
            if device.properties.get('ID_BUS')=="usb":
                check_action('add', device)

    def start_polling(self):
        '''
            Synchronous method for polling activity.
        '''
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
        '''
            Asynchronous method for polling activity.
            If GUI is implemented, tries to use observer as defined here
            https://pyudev.readthedocs.io/en/latest/guide.html#gui-toolkit-integration
        '''
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
        '''
            Shows notification of device add/remove

            Args:
                action (str): Among possible device action add/remove/change/move
                dev (Device): An instance of pyudev Device
        '''
        logging.info(f'''Action: {dev.action}
            ID_FS_LABEL: {dev.properties.get('ID_FS_LABEL')}
            ID_VENDOR: {dev.properties.get('ID_VENDOR')}
            ID_MODEL: {dev.properties.get('ID_MODEL')}
            ID_TYPE: {dev.properties.get('ID_TYPE')}
            ID_BUS: {dev.properties.get('ID_BUS')}
            ID_FS_VERSION: {dev.properties.get('ID_FS_VERSION')}
        ''')

    def destroy(self):
        '''
            Destroys the polling loop
            If asynchronous method is used, sends stop operation
        '''
        self._destruct = True
        if self._async_mode:
            self._async_monitor.stop()


class USBDrive:
    '''
        Class that stores the information of currently inserted USB drives
    '''
    def __init__(self, device):
        self._device = device
        self.storage_info = None
        self.prev_storage_info = None
        logging.debug(f"Device Node: '{self._device.device_node}'")
        self.ID_FS_LABEL = self._device.properties.get('ID_FS_LABEL')
        self.ID_VENDOR = self._device.properties.get('ID_VENDOR')
        self.ID_MODEL = self._device.properties.get('ID_MODEL')
        self.is_mounted = False
        self.get_device_mount_point()

    def get_device_mount_point(self):
        '''
            Reads from /proc/mounts file to check if device is mounted
            Stores the mount point to an object variable and sets is_mounted to True
        '''
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
                        self.is_mounted = True
                        return
        # logging.error('''USB Drive hasn't been mounted. Keeping it in waiting list''')

    def get_storage_information(self):
        '''
            Stores recorded storage info as backup and gets current info

            Returns:
                storage_info (Dict): Returns storage detail containing Total, Free and Used space
        '''
        if self._mount_point is None or not self._mount_point.exists():
            return None
        # else:
        #     with suppress(AttributeError):
        #         logging.debug(f'Exists: {self._mount_point.exists()}')
        self.prev_storage_info = self.storage_info
        self.storage_info = shutil.disk_usage(self._mount_point)
        return self.storage_info

    def get_free_space_changes(self, notify_changes:bool=False):
        '''
            Checks if any change in free_space

            Args:
                notify_changes (bool): If True, displays notification when space change is noticed
            
            Returns:
                change_in_freespace (int): Bytes of free space change
        '''
        if self.prev_storage_info is None or self.storage_info is None:
            return 0
        change_in_freespace = self.prev_storage_info.free - self.storage_info.free
        # logging.debug(f'Free space query: {change_in_freespace}')
        if notify_changes and change_in_freespace:
            self.notify_change(change_in_freespace)
        return change_in_freespace

    def notify_change(self, size):
        '''
            Logs the Space change info
        '''
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
    '''
        A thread that constantly monitors change in free space of connected USBs and adds/removes from dictionary and sleep through POLL_SLEEP_TIME
    '''
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.device_avail = {}
        self._destruct = False

    def create_device(self, device):
        '''
            Adds a device to dictionary with keyname = "device_node"
        '''
        if device.device_node in self.device_avail:
            self.remove_device(device)
        self.device_avail.update({device.device_node: USBDrive(device)})

    def remove_device(self, device):
        '''
            Removes a device from dictionary
        '''
        dev_data = self.device_avail.get(device.device_node)
        if dev_data:
            dev = self.device_avail.pop(device.device_node)
            del dev

    def run(self):
        '''
            Main loop that checks for any new device inserted or removed from queue
            and from available devices checks if free_space has been changed or not
        '''
        while not self._destruct:
            try:
                while dev_queue.qsize() > 0:
                    try:
                        action, device = dev_queue.get(timeout=POLL_SLEEP_TIME)
                        if action == 'add':
                            self.create_device(device)
                        elif action == 'remove':
                            self.remove_device(device)
                    except Empty:
                        break
                for node,usb in self.device_avail.items():
                    try:
                        if not usb.is_mounted:
                            usb.get_device_mount_point() #set is_mounted to True if device has been mounted
                        if usb.is_mounted: #else is not implemented because value can turn True from above statement
                            _ = usb.get_storage_information()
                            usb.get_free_space_changes(notify_changes=True)
                    except AttributeError:
                        logging.error('Attribute error for a device. Deleting from dict')
                        self.remove_device(usb._device)
                sleep(POLL_SLEEP_TIME)
            except (KeyboardInterrupt, StopIteration):
                break

    def destroy(self):
        '''
            Exists from the main run loop
        '''
        self._destruct = True


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

    monitor = USBMonitor()
    poller = Poller()

    logging.debug('''Starting Monitor
        Press Ctrl+C to quit
    ''')
    
    poller.start()

    #monitor.start_polling() #synchronous method
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

    