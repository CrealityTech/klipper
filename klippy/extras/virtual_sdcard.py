# Virtual sdcard support (print files directly from a host g-code file)
#
# Copyright (C) 2018  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import os, logging

VALID_GCODE_EXTS = ['gcode', 'g', 'gco']
LAYER_KEYS = [";LAYER", "; layer", "; LAYER", ";AFTER_LAYER_CHANGE"]

class VirtualSD:
    def __init__(self, config):
        printer = config.get_printer()
        printer.register_event_handler("klippy:shutdown", self.handle_shutdown)
        self.printer = printer
        # sdcard state
        sd = config.get('path')
        self.sdcard_dirname = os.path.normpath(os.path.expanduser(sd))
        self.current_file = None
        self.file_position = self.file_size = 0
        # Print Stat Tracking
        self.print_stats = printer.load_object(config, 'print_stats')
        # Work timer
        self.reactor = printer.get_reactor()
        self.must_pause_work = self.cmd_from_sd = False
        self.next_file_position = 0
        self.work_timer = None
        if printer.start_args.get("apiserver")[-1] != "s":
            self.index = printer.start_args.get("apiserver")[-1]
        else:
            self.index = "1"
        # Register commands
        self.gcode = printer.lookup_object('gcode')
        for cmd in ['M20', 'M21', 'M23', 'M24', 'M25', 'M26', 'M27']:
            self.gcode.register_command(cmd, getattr(self, 'cmd_' + cmd))
        for cmd in ['M28', 'M29', 'M30']:
            self.gcode.register_command(cmd, self.cmd_error)
        self.gcode.register_command(
            "SDCARD_RESET_FILE", self.cmd_SDCARD_RESET_FILE,
            desc=self.cmd_SDCARD_RESET_FILE_help)
        self.gcode.register_command(
            "SDCARD_PRINT_FILE", self.cmd_SDCARD_PRINT_FILE,
            desc=self.cmd_SDCARD_PRINT_FILE_help)
        # self.printer = printer
    def handle_shutdown(self):
        if self.work_timer is not None:
            self.must_pause_work = True
            try:
                readpos = max(self.file_position - 1024, 0)
                readcount = self.file_position - readpos
                self.current_file.seek(readpos)
                data = self.current_file.read(readcount + 128)
            except:
                logging.exception("virtual_sdcard shutdown read")
                return
            logging.info("Virtual sdcard (%d): %s\nUpcoming (%d): %s",
                         readpos, repr(data[:readcount]),
                         self.file_position, repr(data[readcount:]))
    def stats(self, eventtime):
        if self.work_timer is None:
            return False, ""
        return True, "sd_pos=%d" % (self.file_position,)
    def get_file_list(self, check_subdirs=False):
        if check_subdirs:
            flist = []
            for root, dirs, files in os.walk(
                    self.sdcard_dirname, followlinks=True):
                for name in files:
                    ext = name[name.rfind('.')+1:]
                    if ext not in VALID_GCODE_EXTS:
                        continue
                    full_path = os.path.join(root, name)
                    r_path = full_path[len(self.sdcard_dirname) + 1:]
                    size = os.path.getsize(full_path)
                    flist.append((r_path, size))
            return sorted(flist, key=lambda f: f[0].lower())
        else:
            dname = self.sdcard_dirname
            try:
                filenames = os.listdir(self.sdcard_dirname)
                return [(fname, os.path.getsize(os.path.join(dname, fname)))
                        for fname in sorted(filenames, key=str.lower)
                        if not fname.startswith('.')
                        and os.path.isfile((os.path.join(dname, fname)))]
            except:
                logging.exception("virtual_sdcard get_file_list")
                raise self.gcode.error("Unable to get file list")
    def get_status(self, eventtime):
        return {
            'file_path': self.file_path(),
            'progress': self.progress(),
            'is_active': self.is_active(),
            'file_position': self.file_position,
            'file_size': self.file_size,
        }
    def file_path(self):
        if self.current_file:
            return self.current_file.name
        return None
    def progress(self):
        if self.file_size:
            # logging.info("progress:%f, file_position:%s, file_size:%f" % (
            # float(self.file_position) / self.file_size, self.file_position, self.file_size))
            try:
                return float(self.file_position) / self.file_size
            except Exception as e:
                logging.exception(e)
                return 0.
        else:
            return 0.
    def is_active(self):
        return self.work_timer is not None
    def do_pause(self):
        if self.work_timer is not None:
            self.must_pause_work = True
            while self.work_timer is not None and not self.cmd_from_sd:
                self.reactor.pause(self.reactor.monotonic() + .001)
    def do_resume(self):
        if self.work_timer is not None:
            logging.error("do_resume work_timer is not None")
            raise self.gcode.error("""{"code":"key217", "msg": "SD busy" "values": []}""")
        self.must_pause_work = False
        self.work_timer = self.reactor.register_timer(
            self.work_handler, self.reactor.NOW)
    def do_cancel(self):
        if self.current_file is not None:
            self.do_pause()
            self.current_file.close()
            self.current_file = None
            self.print_stats.note_cancel()
        self.file_position = self.file_size = 0.
    # G-Code commands
    def cmd_error(self, gcmd):
        raise gcmd.error("SD write not supported")
    def _reset_file(self):
        if self.current_file is not None:
            self.do_pause()
            self.current_file.close()
            self.current_file = None
        self.file_position = self.file_size = 0.
        self.print_stats.reset()
    cmd_SDCARD_RESET_FILE_help = "Clears a loaded SD File. Stops the print "\
        "if necessary"
    def cmd_SDCARD_RESET_FILE(self, gcmd):
        if self.cmd_from_sd:
            raise gcmd.error(
                """{"code":"key131", "msg": "SDCARD_RESET_FILE cannot be run from the sdcard", "values": []}""")
        self._reset_file()
    cmd_SDCARD_PRINT_FILE_help = "Loads a SD file and starts the print.  May "\
        "include files in subdirectories."
    def cmd_SDCARD_PRINT_FILE(self, gcmd):
        if self.work_timer is not None:
            logging.error("cmd_SDCARD_PRINT_FILE work_timer is not None")
            raise gcmd.error("""{"code":"key217", "msg": "SD busy" "values": []}""")
        self._reset_file()
        filename = gcmd.get("FILENAME")
        if filename[0] == '/':
            filename = filename[1:]
        self._load_file(gcmd, filename, check_subdirs=True)
        self.do_resume()
    def cmd_M20(self, gcmd):
        # List SD card
        files = self.get_file_list()
        gcmd.respond_raw("Begin file list")
        for fname, fsize in files:
            gcmd.respond_raw("%s %d" % (fname, fsize))
        gcmd.respond_raw("End file list")
    def cmd_M21(self, gcmd):
        # Initialize SD card
        gcmd.respond_raw("SD card ok")
    def cmd_M23(self, gcmd):
        # Select SD file
        if self.work_timer is not None:
            logging.error("cmd_M23 work_timer is not None")
            raise gcmd.error("""{"code":"key217", "msg": "SD busy" "values": []}""")
        self._reset_file()
        try:
            orig = gcmd.get_commandline()
            filename = orig[orig.find("M23") + 4:].split()[0].strip()
            if '*' in filename:
                filename = filename[:filename.find('*')].strip()
        except:
            raise gcmd.error("""{"code":"key120", "msg": "Unable to extract filename", "values": []}""")
        if filename.startswith('/'):
            filename = filename[1:]
        self._load_file(gcmd, filename)
    def _load_file(self, gcmd, filename, check_subdirs=False):
        files = self.get_file_list(check_subdirs)
        flist = [f[0] for f in files]
        files_by_lower = { fname.lower(): fname for fname, fsize in files }
        fname = filename
        try:
            if fname not in flist:
                fname = files_by_lower[fname.lower()]
            fname = os.path.join(self.sdcard_dirname, fname)
            f = open(fname, 'r')
            f.seek(0, os.SEEK_END)
            fsize = f.tell()
            f.seek(0)
        except Exception as e:
            # logging.exception("virtual_sdcard file open")
            logging.exception(e)
            raise gcmd.error("""{"code":"key121", "msg": "Unable to open file", "values": []}""")
        gcmd.respond_raw("File opened:%s Size:%d" % (filename, fsize))
        gcmd.respond_raw("File selected")
        self.current_file = f
        self.file_position = 0
        self.file_size = fsize
        self.print_stats.set_current_file(filename)
    def cmd_M24(self, gcmd):
        # Start/resume SD print
        self.do_resume()
    def cmd_M25(self, gcmd):
        # Pause SD print
        self.do_pause()
    def cmd_M26(self, gcmd):
        # Set SD position
        if self.work_timer is not None:
            logging.error("cmd_M26 work_timer is not None")
            raise gcmd.error("SD busy")
        pos = gcmd.get_int('S', minval=0)
        self.file_position = pos
    def cmd_M27(self, gcmd):
        # Report SD print status
        if self.current_file is None:
            gcmd.respond_raw("Not SD printing.")
            return
        gcmd.respond_raw("SD printing byte %d/%d"
                         % (self.file_position, self.file_size))
    def get_file_position(self):
        return self.next_file_position
    def set_file_position(self, pos):
        self.next_file_position = pos
    def is_cmd_from_sd(self):
        return self.cmd_from_sd
    # Background work timer
    def work_handler(self, eventtime):
        import time
        # When the nozzle is moved
        try:
            import yaml
            with open("/mnt/UDISK/.crealityprint/time_lapse.yaml") as f:
                config_data = yaml.load(f.read(), Loader=yaml.Loader)
            # if timelapse_position == 1 then When the nozzle is moved
            timelapse_postion = int(config_data.get('1').get("position", 0))
            enable_delay_photography = config_data.get('1').get("enable_delay_photography", False)
            frequency = int(config_data.get("1").get("frequency", 1))
            # if timelapse_postion == 0:
            #     frequency = int(config_data.get("1").get("frequency", 1))
            # else:
            #     frequency = 1
        except Exception as e:
            logging.exception(e)
            timelapse_postion = 0
            frequency = 1
            enable_delay_photography = False

        layer_count = 0
        video0_status = True
        logging.info("get enable_delay_photography:%s timelapse position is %s" % (enable_delay_photography, timelapse_postion))
        logging.info("Starting SD card print (position %d)", self.file_position)

        self.reactor.unregister_timer(self.work_timer)
        try:
            self.current_file.seek(self.file_position)
        except:
            logging.exception("virtual_sdcard seek")
            self.work_timer = None
            return self.reactor.NEVER
        self.print_stats.note_start()
        gcode_mutex = self.gcode.get_mutex()
        partial_input = ""
        lines = []
        error_message = None
        while not self.must_pause_work:
            if not lines:
                # Read more data
                try:
                    data = self.current_file.read(8192)
                except:
                    logging.exception("virtual_sdcard read")
                    break
                if not data:
                    # End of file
                    self.current_file.close()
                    self.current_file = None
                    logging.info("Finished SD card print")
                    self.gcode.respond_raw("Done printing file")
                    break
                lines = data.split('\n')
                lines[0] = partial_input + lines[0]
                partial_input = lines.pop()
                lines.reverse()
                self.reactor.pause(self.reactor.NOW)
                continue
            # Pause if any other request is pending in the gcode class
            if gcode_mutex.test():
                self.reactor.pause(self.reactor.monotonic() + 0.100)
                continue
            # Dispatch command
            self.cmd_from_sd = True
            line = lines.pop()
            next_file_position = self.file_position + len(line) + 1
            self.next_file_position = next_file_position
            try:
                # logging.info(line)
                if enable_delay_photography == True and video0_status == True:
                    for layer_key in LAYER_KEYS:
                        if line.startswith(layer_key):
                            if layer_count % int(frequency) == 0:
                                if not os.path.exists("/dev/video0"):
                                    video0_status = False
                                    continue
                                line = "TIMELAPSE_TAKE_FRAME"
                                # logging.info("timelapse_postion: %d" % timelapse_postion)
                                # logging.info(line)
                                # if timelapse_postion:
                                #     toolhead = self.printer.lookup_object('toolhead')
                                #     X, Y, Z, E = toolhead.get_position()
                                #     # 1. Pull back and lift first
                                #     cmd_list1 = ["M83", "G1 E-4", "M82"]
                                #     for sub_cmd in cmd_list1:
                                #         logging.info(sub_cmd)
                                #         self.gcode.run_script(sub_cmd)
                                #     time.sleep(0.8)
                                #     cmd_list2 = ["G91", "G1 Z2", "G90"]
                                #     for sub_cmd in cmd_list2:
                                #         logging.info(sub_cmd)
                                #         self.gcode.run_script(sub_cmd)
                                #     time.sleep(0.4)
                                #
                                #     # 2. move to the specified position
                                #     cmd = "G0 X5 Y150 F9000"
                                #     logging.info(cmd)
                                #     self.gcode.run_script(cmd)
                                #     cmd_wait_for_stepper = "M400"
                                #     logging.info(cmd_wait_for_stepper)
                                #     self.gcode.run_script(cmd_wait_for_stepper)
                                #
                                #     # 3. move back
                                #     # cmd_list3 = ["M83", "G1 E3", "M82"]
                                #     # for sub_cmd in cmd_list3:
                                #     #     logging.info(sub_cmd)
                                #     #     self.gcode.run_script(sub_cmd)
                                #     time.sleep(0.4)
                                #     cmd_list4 = ["G91", "G1 Z-2", "G90"]
                                #     for sub_cmd in cmd_list4:
                                #         logging.info(sub_cmd)
                                #         self.gcode.run_script(sub_cmd)
                                #     move_back_cmd = "G1 X%s Y%s Z%s F10000" % (X, Y, Z)
                                #     logging.info(move_back_cmd)
                                #     self.gcode.run_script(move_back_cmd)
                            layer_count += 1
                            break
                self.gcode.run_script(line)
            except self.gcode.error as e:
                error_message = str(e)
                break
            except:
                logging.exception("virtual_sdcard dispatch")
                break
            self.cmd_from_sd = False
            self.file_position = self.next_file_position
            # Do we need to skip around?
            if self.next_file_position != next_file_position:
                try:
                    self.current_file.seek(self.file_position)
                except:
                    logging.exception("virtual_sdcard seek")
                    self.work_timer = None
                    return self.reactor.NEVER
                lines = []
                partial_input = ""
        logging.info("Exiting SD card print (position %d)", self.file_position)

        self.work_timer = None
        self.cmd_from_sd = False
        if error_message is not None:
            self.print_stats.note_error(error_message)
            # import threading
            # t = threading.Thread(target=self._last_reset_file)
            # t.start()
        elif self.current_file is not None:
            self.print_stats.note_pause()
        else:
            self.print_stats.note_complete()
            import threading
            t = threading.Thread(target=self._last_reset_file)
            t.start()
        return self.reactor.NEVER

    def _last_reset_file(self):
        logging.info("will use _last_rest_file after 5s...")
        import time
        time.sleep(5)
        logging.info("use _last_rest_file")
        self._reset_file()

    def get_yaml_info(self, _config_file=None):
        """
        read yaml file info
        """
        import yaml
        # if not _config_file:
        if not os.path.exists(_config_file):
            return {}
        config_data = {}
        try:
            with open(_config_file, 'r') as f:
                config_data = yaml.load(f.read(), Loader=yaml.Loader)
        except Exception as err:
            pass
        return config_data

    def set_yaml_info(self, _config_file=None, data=None):
        """
        write yaml file info
        """
        import yaml
        if not _config_file:
            return
        try:
            with open(_config_file, 'w+') as f:
                yaml.dump(data, f, allow_unicode=True)
                f.flush()
            os.system("sync")
        except Exception as e:
            pass

def load_config(config):
    return VirtualSD(config)
