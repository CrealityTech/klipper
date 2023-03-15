# Virtual sdcard support (print files directly from a host g-code file)
#
# Copyright (C) 2018  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import imp
import os, logging

VALID_GCODE_EXTS = ['gcode', 'g', 'gco']
LAYER_KEYS = [";LAYER:", "; layer:", "; LAYER:", ";AFTER_LAYER_CHANGE"]

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
        self.start_time = 0
        self.end_time = 0
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
        self.count = 0
        self.count_G1 = 0
        self.do_resume_status = False
        self.do_cancel_status = False
        self.create_video_params = {}
        self.cancel_print_state = False
        self.fan_state = ""
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
        if self.must_pause_work:
            self.create_video()
        self.do_cancel_status = True
        if self.current_file is not None:
            self.do_pause()
            self.current_file.close()
            self.current_file = None
            self.print_stats.note_cancel()
        self.file_position = self.file_size = 0.
        from subprocess import call
        mcu = self.printer.lookup_object('mcu', None)
        pre_serial = mcu._serial.serial_dev.port.split("/")[-1]
        path = "/mnt/UDISK/%s_gcode_coordinate.save" % pre_serial
        print_file_name_save_path = "/mnt/UDISK/%s_print_file_name.save" % pre_serial
        if os.path.exists(path):
            os.remove(path)
        if os.path.exists(print_file_name_save_path):
            os.remove(print_file_name_save_path)
        call("sync", shell=True)

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
        logging.error("File opened:%s Size:%d start_print" % (filename, fsize))
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

    def record_status(self, path, line_pos):
        gcode_move = self.printer.lookup_object('gcode_move')
        gcode_move.cmd_CX_SAVE_GCODE_STATE(self.file_position, path, line_pos)

    def create_video(self):
        try:
            if not self.create_video_params.get("enable_delay_photography", True):
                return
            if not self.create_video_params.get("isCurUSB", True):
                return
            timelapse_postion = self.create_video_params.get("timelapse_postion")
            layer_count = self.create_video_params.get("layer_count")
            output_framerate = self.create_video_params.get("output_framerate")
            frequency = self.create_video_params.get("frequency")
            base_shoot_path = self.create_video_params.get("base_shoot_path")
            output_pre_video_path = self.create_video_params.get("output_pre_video_path")
            filename = self.create_video_params.get("filename")
            test_jpg_path = self.create_video_params.get("test_jpg_path")
            from datetime import datetime
            now = datetime.now()
            date_time = now.strftime("%Y%m%d_%H%M")
            # 20220121010735@False@1@15@.mp4
            camera_site = True if timelapse_postion == 1 else False
            # filename_extend = f"@{camera_site}@{frequency}@{output_framerate}@"
            play_times = int(layer_count / int(frequency) / output_framerate)
            filename_extend = "@%s@%s@%s@%s@" % (camera_site, frequency, output_framerate, play_times)
            outfile = "timelapse_%s_%s%s" % (filename, date_time, filename_extend)
            rendering_video_cmd = """ffmpeg -framerate {0} -i  {1} -vcodec copy -y -f mp4 '{2}.mp4'""".format(
                output_framerate, base_shoot_path, output_pre_video_path + "/" + outfile)
            preview_jpg_path = test_jpg_path.replace("test.jpg", outfile + ".jpg")
            snapshot_cmd = "wget http://localhost:8080/?action=snapshot -O '%s'" % preview_jpg_path
            logging.info(snapshot_cmd)
            os.system(snapshot_cmd)
            logging.info(rendering_video_cmd)
            os.system(rendering_video_cmd)
        except Exception as e:
            logging.exception(e)

    def tail_read(self, f):
        cur_pos = f.tell()
        buf = b''
        while True:
            b = f.read(1)
            buf = b + buf
            cur_pos -= 1
            if cur_pos < 0: break
            f.seek(cur_pos)
            if b.startswith("\n") or b.startswith("\r"):
                buf = b'\n'
            if (buf.startswith("G1") or buf.startswith("G0") or buf.startswith(";")) and buf.endswith("\n"):
                break
        return buf

    def getXYZE(self, file_path, file_position):
        result = {"X": 0, "Y": 0, "Z": 0, "E": 0}
        try:
            import io
            with io.open(file_path, "r", encoding="utf-8") as f:
                f.seek(file_position)
                while True:
                    cur_pos = f.tell()
                    if cur_pos<=0:
                        break
                    line = self.tail_read(f)
                    line_list = line.split(" ")
                    if not result["E"] and "E" in line:
                        for obj in line_list:
                            if obj.startswith("E"):
                                ret = obj[1:].split("\r")[0]
                                ret = ret.split("\n")[0]
                                if ret.startswith("."):
                                    result["E"] = float(("0"+ret.strip(" ")))
                                else:
                                    result["E"] = float(ret.strip(" "))
                    if not result["X"] and not result["Y"]:
                        for obj in line_list:
                            if obj.startswith("X"):
                                result["X"] = float(obj.split("\r")[0][1:])
                            if obj.startswith("Y"):
                                result["Y"] = float(obj.split("\r")[0][1:])
                    if not result["Z"] and "Z" in line:
                        for obj in line_list:
                            if obj.startswith("Z"):
                                result["Z"] = float(obj.split("\r")[0][1:])
                    if result["X"] and result["Y"] and result["Z"] and result["E"]:
                        logging.info("get XYZE:%s" % str(result))
                        break
        except Exception as err:
            logging.exception(err)
        return result

    def get_print_temperature(self, file_path):
        import re
        bed = 0
        nozzle = 0
        try:
            with open(file_path, "r") as f:
                count = 50000
                while count > 0:
                    count -= 1
                    line = f.readline()
                    M109_state = re.findall(r"M109 S(.+?)\n", line)
                    if M109_state:
                        nozzle = int(M109_state[0])
                        continue
                    M190_state = re.findall(r"M190 S(.+?)\n", line)
                    if M190_state:
                        bed = int(M190_state[0])
                        continue
                    M104_state = re.findall(r"M104 S(.+?)\n", line)
                    if M104_state:
                        nozzle = int(M104_state[0])
                        continue
                    M140_state = re.findall(r"M140 S(.+?)\n", line)
                    if M140_state:
                        bed = int(M140_state[0])
                        continue
                    if bed and nozzle:
                        break
        except Exception as err:
            bed = 60
            nozzle = 200
            logging.error(err)
        return bed, nozzle

    # Background work timer
    def work_handler(self, eventtime):
        # self.print_stats.note_start()
        import time
        # When the nozzle is moved
        output_pre_video_path = "/mnt/UDISK/.crealityprint/video"
        try:
            import yaml, os
            with open("/mnt/UDISK/.crealityprint/time_lapse.yaml") as f:
                config_data = yaml.load(f.read(), Loader=yaml.Loader)
            # if timelapse_position == 1 then When the nozzle is moved
            timelapse_postion = int(config_data.get('1').get("position", 0))
            enable_delay_photography = config_data.get('1').get("enable_delay_photography", False)
            frequency = int(config_data.get("1").get("frequency", 1))
            z_upraise = int(config_data.get('1').get("z_upraise", 1))
            brain_fps = config_data.get("1").get("fps", "MP4-15")
            usb_serial = "usb_serial_%s" % config_data.get("1").get("usb", "1")
            if brain_fps == "MP4-15":
                output_framerate = 15
            else:
                output_framerate = 25
            filename = os.path.basename(self.file_path())
            extruder =  0 - float(config_data.get('1').get("extruder", 3))
            extruder_speed =  int(config_data.get('1').get("extruder_speed", 40)) * 60
        except Exception as e:
            logging.exception(e)
            import os
            filename = os.path.basename(self.file_path())
            timelapse_postion = 0
            frequency = 1
            enable_delay_photography = False
            usb_serial = "usb_serial_1"
            z_upraise = 1
            extruder = -3
            extruder_speed =  40*60
            output_framerate = 15

        mcu = self.printer.lookup_object('mcu', None)
        pre_serial = mcu._serial.serial_dev.port.split("/")[-1]
        base_shoot_path = "/mnt/UDISK/delayed_imaging/test.264"
        test_jpg_path = output_pre_video_path + "/test.jpg"
        path = "/mnt/UDISK/%s_gcode_coordinate.save" % pre_serial
        try:
            if not self.do_resume_status and not os.path.exists(path) and enable_delay_photography and pre_serial == usb_serial:
                rm_video = "rm -f " + base_shoot_path
                logging.info(rm_video)
                os.system(rm_video)
        except:
            pass

        layer_count = 0
        video0_status = True
        logging.info("get enable_delay_photography:%s timelapse position is %s" % (enable_delay_photography, timelapse_postion))
        logging.info("Starting SD card print (position %d)", self.file_position)


        import os, json
        # path = "/mnt/UDISK/%s_gcode_coordinate.save" % pre_serial
        print_file_name_save_path = "/mnt/UDISK/%s_print_file_name.save" % pre_serial
        path2 = "/mnt/UDISK/.crealityprint/print_switch.txt"
        print_switch = False
        if os.path.exists(path2):
            try:
                with open(path2, "r") as f:
                    ret = json.loads(f.read())
                    print_switch = ret.get("switch", False)
            except Exception as err:
                pass

        state = {}

        if print_switch and not self.do_resume_status and os.path.exists(path):
            try:
                self.print_stats.note_start(info_path=print_file_name_save_path)
                with open(path, "r") as f:
                    ret = f.readlines()
                    info = {}
                    for obj in ret:
                        obj = obj.strip("'").strip("\n")
                        if len(obj) > 10:
                            obj = eval(obj)
                            if not info:
                                info = obj
                            else:
                                if obj.get("file_position", 0) > info.get("file_position", 0):
                                    info = obj
                    state = info
                    # state = json.loads(f.read())
                    self.file_position = int(state.get("file_position", 0))
                    gcode = self.printer.lookup_object('gcode')
                    temperature = self.get_print_temperature(self.current_file.name)
                    gcode.run_script("M140 S%s" % temperature[0])
                    gcode.run_script("M109 S%s" % temperature[1])
                    # gcode.run_script("M140 S60")
                    # gcode.run_script("M109 S200")

                    if not self.cancel_print_state:
                        gcode_move = self.printer.lookup_object('gcode_move', None)
                        XYZE = self.getXYZE(self.current_file.name, self.file_position)
                        gcode_move.cmd_CX_RESTORE_GCODE_STATE(path, print_file_name_save_path, XYZE)
                    else:
                        if os.path.exists(path):
                            os.remove(path)
                        if os.path.exists(print_file_name_save_path):
                            os.remove(print_file_name_save_path)
            except Exception as err:
                logging.exception(err)
        else:
            self.print_stats.note_start()
        if print_switch:
            gcode_move = self.printer.lookup_object('gcode_move')
            gcode_move.recordPrintFileName(print_file_name_save_path, self.current_file.name)
        def create_video(timelapse_postion, layer_count, output_framerate, frequency, base_shoot_path, output_pre_video_path):
            try:
                # outfile = f"timelapse_{gcodefilename}_{date_time}{filename_extend}"
                from datetime import datetime
                now = datetime.now()
                date_time = now.strftime("%Y%m%d_%H%M")
                # 20220121010735@False@1@15@.mp4
                camera_site = True if timelapse_postion == 1 else False
                # filename_extend = f"@{camera_site}@{frequency}@{output_framerate}@"
                play_times = int(layer_count / int(frequency) / output_framerate)
                filename_extend = "@%s@%s@%s@%s@" % (camera_site, frequency, output_framerate, play_times)
                outfile = "timelapse_%s_%s%s" % (filename, date_time, filename_extend)
                rendering_video_cmd = """ffmpeg -framerate {0} -i  {1} -vcodec copy -y -f mp4 '{2}.mp4'""".format(
                    output_framerate, base_shoot_path, output_pre_video_path + "/" + outfile)
                preview_jpg_path = test_jpg_path.replace("test.jpg", outfile + ".jpg")
                snapshot_cmd = "wget http://localhost:8080/?action=snapshot -O '%s'" % preview_jpg_path
                logging.info(snapshot_cmd)
                os.system(snapshot_cmd)
                logging.info(rendering_video_cmd)
                os.system(rendering_video_cmd)
            except Exception as e:
                logging.exception(e)
        self.reactor.unregister_timer(self.work_timer)
        try:
            self.current_file.seek(self.file_position)
        except:
            logging.exception("virtual_sdcard seek")
            self.work_timer = None
            return self.reactor.NEVER
        # self.print_stats.note_start()
        gcode_mutex = self.gcode.get_mutex()
        partial_input = ""
        lines = []
        error_message = None
        gcode_move = self.printer.lookup_object('gcode_move')
        lastE = 0
        line_pos = 1
        isCurUSB = True if pre_serial == usb_serial else False
        self.create_video_params = {"timelapse_postion": timelapse_postion, "layer_count": layer_count, "output_framerate": output_framerate, "frequency": frequency,
                                    "base_shoot_path": base_shoot_path, "output_pre_video_path": output_pre_video_path, "filename": filename, "test_jpg_path": test_jpg_path,
                                    "isCurUSB": isCurUSB, "enable_delay_photography": enable_delay_photography}
        self.start_time = int(time.time())
        end_filename = self.file_path()
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
                    if os.path.exists(path):
                        os.remove(path)
                    if os.path.exists(print_file_name_save_path):
                        os.remove(print_file_name_save_path)
                    if not self.do_resume_status and enable_delay_photography and pre_serial == usb_serial:
                        create_video(timelapse_postion, layer_count, output_framerate, frequency, base_shoot_path, output_pre_video_path)
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
                if print_switch and self.count_G1 >= 20 and self.count % 9 == 0:
                    if not os.path.exists(path):
                        with open(path, "w") as f:
                            f.writelines([" \n", " "])
                            f.flush()
                    self.record_status(path, line_pos)
                    if line_pos == 1:
                        line_pos = 2
                    else:
                        line_pos = 1
                if print_switch and self.count_G1 == 19:
                    gcode_move.recordPrintFileName(print_file_name_save_path, self.current_file.name, fan_state=self.fan_state, filament_used=self.print_stats.filament_used, last_print_duration=self.print_stats.print_duration)
                if print_switch and self.count % 29 == 0:
                    gcode_move.recordPrintFileName(print_file_name_save_path, self.current_file.name, fan_state=self.fan_state, filament_used=self.print_stats.filament_used, last_print_duration=self.print_stats.print_duration)

                # logging.info(line)
                if line.startswith("G1") and "E" in line:
                    try:
                        E_str = line.split(" ")[-1]
                        if E_str.startswith("E"):
                            lastE = float(E_str.strip("\r").strip("\n")[1:])
                    except Exception as err:
                        pass
                elif line.startswith("M106"):
                    self.fan_state = line.strip("\r").strip("\n")
                    if print_switch:
                        gcode_move.recordPrintFileName(print_file_name_save_path, self.current_file.name, fan_state=self.fan_state)
                if video0_status == False and os.path.exists("/dev/video0"):
                    video0_status = True
                if enable_delay_photography == True and video0_status == True and pre_serial == usb_serial:
                    for layer_key in LAYER_KEYS:
                        if ";LAYER_COUNT:" in layer_key:
                            break
                        if line.startswith(layer_key):
                            if layer_count % int(frequency) == 0:
                                if not os.path.exists("/dev/video0"):
                                    video0_status = False
                                    continue
                                # line = "TIMELAPSE_TAKE_FRAME"
                                logging.info("timelapse_postion: %d" % timelapse_postion)
                                # logging.info(line)
                                if timelapse_postion:
                                    cmd_wait_for_stepper = "M400"
                                    toolhead = self.printer.lookup_object('toolhead')
                                    X, Y, Z, E = toolhead.get_position()
                                    if self.count_G1 >= 20:
                                        # 1. Pull back and lift first
                                        logging.info("G1 F%s E%s" % (extruder_speed, lastE+extruder))
                                        logging.info(cmd_wait_for_stepper)
                                        self.gcode.run_script("G1 F%s E%s" % (extruder_speed, lastE+extruder))
                                        self.gcode.run_script(cmd_wait_for_stepper)
                                        time.sleep(0.1)
                                        logging.info("G1 F3000 Z%s" % (Z + z_upraise))
                                        logging.info(cmd_wait_for_stepper)
                                        self.gcode.run_script("G1 F3000 Z%s" % (Z + z_upraise))
                                        self.gcode.run_script(cmd_wait_for_stepper)
                                        time.sleep(0.1)
                                        # 2. move to the specified position
                                        cmd = "G0 X5 Y150 F15000"
                                        logging.info(cmd)
                                        logging.info(cmd_wait_for_stepper)
                                        self.gcode.run_script(cmd)
                                        self.gcode.run_script(cmd_wait_for_stepper)
                                        try:
                                            capture_shell = "capture"
                                            logging.info(capture_shell)
                                            os.system(capture_shell)
                                            # if not os.path.exists(test_jpg_path):
                                            #     snapshot_cmd = "wget http://localhost:8080/?action=snapshot -O %s" % test_jpg_path
                                            #     logging.info(snapshot_cmd)
                                            #     os.system(snapshot_cmd)
                                        except:
                                            pass
                                        time.sleep(0.1)
                                        # 3. move back
                                        move_back_cmd = "G0 X%s Y%s F15000" % (X, Y)
                                        logging.info(move_back_cmd)
                                        logging.info(cmd_wait_for_stepper)
                                        self.gcode.run_script(move_back_cmd)
                                        self.gcode.run_script(cmd_wait_for_stepper)
                                        time.sleep(0.2)
                                        logging.info("G1 F3000 Z%s" % Z)
                                        logging.info(cmd_wait_for_stepper)
                                        self.gcode.run_script("G1 F3000 Z%s" % Z)
                                        self.gcode.run_script(cmd_wait_for_stepper)
                                        time.sleep(0.1)
                                        logging.info("G1 F%s E%s" % (extruder_speed, lastE))
                                        self.gcode.run_script("G1 F%s E%s" % (extruder_speed, lastE))
                                else:
                                    try:
                                        capture_shell = "capture &"
                                        logging.info(capture_shell)
                                        os.system(capture_shell)
                                        # if not os.path.exists(test_jpg_path):
                                        #     snapshot_cmd = "wget http://localhost:8080/?action=snapshot -O %s" % test_jpg_path
                                        #     logging.info(snapshot_cmd)
                                        #     os.system(snapshot_cmd)
                                    except:
                                        pass
                            layer_count += 1
                            break
                self.gcode.run_script(line)
                self.count += 1
                if self.count_G1 < 20 and line.startswith("G1"):
                    self.count_G1 += 1
            except self.gcode.error as e:
                if os.path.exists(path):
                    os.remove(path)
                if os.path.exists(print_file_name_save_path):
                    os.remove(print_file_name_save_path)
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
        if self.do_cancel_status and enable_delay_photography and pre_serial == usb_serial:
            create_video(timelapse_postion, layer_count, output_framerate, frequency, base_shoot_path, output_pre_video_path)
        logging.info("Exiting SD card print (position %d)", self.file_position)
        logging.error("filename:%s end print", end_filename)

        self.count = 0
        self.count_G1 = 0
        state = {}
        self.do_resume_status = False
        self.do_cancel_status = False

        self.work_timer = None
        self.cmd_from_sd = False
        if error_message is not None:
            self.print_stats.note_error(error_message)
            logging.error("file:" + self.file_path() + ",error:" + error_message)
            self.end_time = int(time.time())
            # print_error_exit|4|/root/gcode_files/1minFK.gcode|1677043800|1677044697|1|{"code":"key22", "msg":"No trigger on %s after full movement", "values": ["x"]}
            msg = "print_error_exit|%s|%s|%s|%s|1|%s" % (
                str(self.index), end_filename, self.start_time, self.end_time, error_message)
            logging.error(msg)
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
