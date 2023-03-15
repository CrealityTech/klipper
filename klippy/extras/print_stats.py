# Virtual SDCard print stat tracking
#
# Copyright (C) 2020  Eric Callahan <arksine.code@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

class PrintStats:
    def __init__(self, config):
        printer = config.get_printer()
        self.gcode_move = printer.load_object(config, 'gcode_move')
        self.reactor = printer.get_reactor()
        self.reset()
        if printer.start_args.get("apiserver")[-1] != "s":
            self.index = printer.start_args.get("apiserver")[-1]
        else:
            self.index = "1"
        self.last_new_total_print_time = self.last_total_print_time = self.new_total_print_time = self.get_last_total_print_time()
        self.print_duration = 0
    def _update_filament_usage(self, eventtime):
        gc_status = self.gcode_move.get_status(eventtime)
        cur_epos = gc_status['position'].e
        self.filament_used += (cur_epos - self.last_epos) \
            / gc_status['extrude_factor']
        self.last_epos = cur_epos
    def set_current_file(self, filename):
        self.reset()
        self.filename = filename
    def note_start(self, info_path=""):
        import os, json
        curtime = self.reactor.monotonic()
        gc_status = self.gcode_move.get_status(curtime)
        if info_path and os.path.exists(info_path):
            ret = {}
            try:
                with open(info_path, "r") as f:
                    ret = json.loads(f.read())
                    self.filament_used = ret.get("filament_used", 0)
            except Exception as err:
                pass
        self.last_epos = gc_status['position'].e
        if self.print_start_time is None:
            if info_path and ret and ret.get("last_print_duration"):
                self.print_start_time = curtime - int(ret.get("last_print_duration"))
            else:
                self.print_start_time = curtime
        elif self.last_pause_time is not None:
            # Update pause time duration
            pause_duration = curtime - self.last_pause_time
            self.prev_pause_duration += pause_duration
            self.last_pause_time = None
        self.state = "printing"
        self.error_message = ""
        self.last_new_total_print_time = self.last_total_print_time = self.new_total_print_time = self.get_last_total_print_time()
    def note_pause(self):
        if self.last_pause_time is None:
            curtime = self.reactor.monotonic()
            self.last_pause_time = curtime
            # update filament usage
            self._update_filament_usage(curtime)
        if self.state != "error":
            self.state = "paused"
    def note_complete(self):
        self._note_finish("complete")
    def note_error(self, message):
        self._note_finish("error", message)
    def note_cancel(self):
        self._note_finish("cancelled")
    def _note_finish(self, state, error_message = ""):
        if self.print_start_time is None:
            return
        self.state = state
        self.error_message = error_message
        eventtime = self.reactor.monotonic()
        self.total_duration = eventtime - self.print_start_time
        if self.filament_used < 0.0000001:
            # No positive extusion detected during print
            self.init_duration = self.total_duration - \
                self.prev_pause_duration
        self.print_start_time = None
    def reset(self):
        self.filename = self.error_message = ""
        self.state = "standby"
        self.prev_pause_duration = self.last_epos = 0.
        self.filament_used = self.total_duration = 0.
        self.print_start_time = self.last_pause_time = None
        self.init_duration = 0.
    def get_status(self, eventtime):
        time_paused = self.prev_pause_duration
        if self.print_start_time is not None:
            if self.last_pause_time is not None:
                # Calculate the total time spent paused during the print
                time_paused += eventtime - self.last_pause_time
            else:
                # Accumulate filament if not paused
                self._update_filament_usage(eventtime)
            self.total_duration = eventtime - self.print_start_time
            if self.filament_used < 0.0000001:
                # Track duration prior to extrusion
                self.init_duration = self.total_duration - time_paused
        print_duration = self.total_duration - self.init_duration - time_paused
        self.print_duration = print_duration
        self.new_total_print_time = print_duration/60 + self.last_total_print_time
        if self.new_total_print_time > self.last_new_total_print_time:
            self.set_total_print_time(self.new_total_print_time)
            self.last_new_total_print_time = self.new_total_print_time
            # self.last_total_print_time = self.new_total_print_time
        return {
            'filename': self.filename,
            'total_duration': self.total_duration,
            'print_duration': print_duration,
            'filament_used': self.filament_used,
            'state': self.state,
            'message': self.error_message
        }

    def get_last_total_print_time(self):
        try:
            with open('/mnt/UDISK/.crealityprint/printer%s_totaltime' % self.index) as f:
                return int(f.read())
        except:
            return 0

    def set_total_print_time(self, new_total_print_time):
        try:
            with open('/mnt/UDISK/.crealityprint/printer%s_totaltime' % self.index, "w+") as f:
                f.write(str(int(new_total_print_time)))
        except:
            pass

def load_config(config):
    return PrintStats(config)
