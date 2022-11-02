# Support for tracking canbus node ids
#
# Copyright (C) 2021  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

class PrinterCANBus:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.ids = {}
    def add_uuid(self, config, canbus_uuid, canbus_iface):
        if canbus_uuid in self.ids:
            raise config.error("""{"code":"key29", "msg":"Duplicate canbus_uuid", "values": []}""")
        new_id = len(self.ids)
        self.ids[canbus_uuid] = new_id
        return new_id
    def get_nodeid(self, canbus_uuid):
        if canbus_uuid not in self.ids:
            raise self.printer.config_error("""{"code":"key30", "msg":"Unknown canbus_uuid %s", "values": ["%s"]}"""
                                            % (canbus_uuid, canbus_uuid))
        return self.ids[canbus_uuid]

def load_config(config):
    return PrinterCANBus(config)
