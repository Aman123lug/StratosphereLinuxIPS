# Stratosphere Linux IPS. A machine-learning Intrusion Detection System
# Copyright (C) 2021 Sebastian Garcia

# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
# Contact: eldraco@gmail.com, sebastian.garcia@agents.fel.cvut.cz, stratosphere@aic.fel.cvut.cz

import multiprocessing
import sys
import os
from datetime import datetime
from watchdog.observers import Observer
from filemonitor import FileEventHandler
from slips.core.database import __database__
import configparser
import time
import json
import traceback


# Input Process
class InputProcess(multiprocessing.Process):
    """ A class process to run the process of the flows """
    def __init__(self, outputqueue, profilerqueue, input_type,
                 input_information, config, packet_filter, zeek_or_bro):
        multiprocessing.Process.__init__(self)
        self.outputqueue = outputqueue
        self.profilerqueue = profilerqueue
        self.config = config
        # Start the DB
        __database__.start(self.config)
        self.input_type = input_type
        self.input_information = input_information
        self.zeek_folder = './zeek_files'
        self.nfdump_output_file = 'nfdump_output.txt'
        self.nfdump_timeout = None
        self.name = 'input'
        self.zeek_or_bro = zeek_or_bro
        self.read_lines_delay = 0
        # Read the configuration
        self.read_configuration()
        # If we were given something from command line, has preference
        # over the configuration file
        if packet_filter:
            self.packet_filter = "'" + packet_filter + "'"
        self.event_handler = None
        self.event_observer = None

    def read_configuration(self):
        """ Read the configuration file for what we need """
        # Get the pcap filter
        try:
            self.packet_filter = self.config.get('parameters', 'pcapfilter')
        except (configparser.NoOptionError, configparser.NoSectionError, NameError):
            # There is a conf, but there is no option, or no section or no configuration file specified
            self.packet_filter = 'ip or not ip'
        # Get tcp inactivity timeout
        try:
            self.tcp_inactivity_timeout = self.config.get('parameters', 'tcp_inactivity_timeout')
        except (configparser.NoOptionError, configparser.NoSectionError, NameError):
            # There is a conf, but there is no option, or no section or no configuration file specified
            self.tcp_inactivity_timeout = ''

    def print(self, text, verbose=1, debug=0):
        """
        Function to use to print text using the outputqueue of slips.
        Slips then decides how, when and where to print this text by taking all the prcocesses into account

        Input
         verbose: is the minimum verbosity level required for this text to be printed
         debug: is the minimum debugging level required for this text to be printed
         text: text to print. Can include format like 'Test {}'.format('here')

        If not specified, the minimum verbosity level required is 1, and the minimum debugging level is 0
        """

        # self.name = f'{Fore.YELLOW}{self.name}{Style.RESET_ALL}'
        vd_text = str(int(verbose) * 10 + int(debug))
        self.outputqueue.put(vd_text + '|' + self.name + '|[' + self.name + '] ' + str(text))

    def read_nfdump_file(self) -> int:
        """
        A binary file generated by nfcapd can read by nfdump.
        The task for this function is watch the nfdump file and if any new line is there, read it.
        """
        file_handler = None
        next_line = None
        last_updated_file_time = datetime.now()
        lines = 0
        line = {}
        line['type'] = 'nfdump'
        while True:
            if not file_handler:
                # We will open here because we do not know when nfdump will open the file.
                try:
                    file_handler = open(self.nfdump_output_file, 'r')
                except FileNotFoundError:
                    # Tryto wait for nfdump to generate output file.
                    time.sleep(1)
                    self.print('The output file for nfdump is still not created.', 0, 1)
                    continue

            if next_line is None:
                # Try to read next line from input file.
                nfdump_line = file_handler.readline()
                if nfdump_line:
                    # We have something to read.
                    # Is this line a valid line?
                    try:
                        # The first item of nfdump output is timestamp.
                        # So the first letter of timestamp should be digit.
                        ts = nfdump_line.split(',')[0]
                        if not ts[0].isdigit():
                            # The first letter is not digit -> not valid line.
                            # TODO: What is this valid line check?? explain
                            continue
                    except IndexError:
                        # There is no first item in  the line.
                        continue

                    # We have a new line.
                    last_updated_file_time = datetime.now()
                    next_line = nfdump_line
                else:
                    # There is no new line.
                    if nfdump_line is None:
                        # Verify that we didn't have any new lines in the last TIMEOUT seconds.
                        now = datetime.now()
                        diff = now - last_updated_file_time
                        diff = diff.seconds
                        if diff >= self.nfdump_timeout:
                            # Stop the reading of the file.
                            break

                    # No new line. Continue.
                    continue
            line['data'] = next_line
            self.print("	> Sent Line: {}".format(line), 0, 3)
            self.profilerqueue.put(line)
            # print('sending new line: {}'.format(next_line))
            next_line = None
            lines += 1

        file_handler.close()
        return lines

    def read_zeek_files(self) -> int:
        # Get the zeek files in the folder now
        zeek_files = __database__.get_all_zeek_file()
        open_file_handlers = {}
        time_last_lines = {}
        cache_lines = {}
        # Try to keep track of when was the last update so we stop this reading
        last_updated_file_time = datetime.now()
        lines = 0

        while True:
            # Go to all the files generated by Zeek and read them
            for filename in zeek_files:
                # Update which files we know about
                try:
                    file_handler = open_file_handlers[filename]
                    # We already opened this file
                    # self.print(f'Old File found {filename}', 0, 6)
                except KeyError:
                    # First time we opened this file.
                    # Ignore the files that do not contain data.
                    if ('capture_loss' in filename or 'loaded_scripts' in filename
                            or 'packet_filter' in filename or 'stats' in filename
                            or 'weird' in filename or 'reporter' in filename):
                        continue
                    file_handler = open(filename + '.log', 'r')
                    open_file_handlers[filename] = file_handler
                    # self.print(f'New File found {filename}', 0, 6)

                # Only read the next line if the previous line was sent
                try:
                    _ = cache_lines[filename]
                    # We have still something to send, do not read the next line from this file
                except KeyError:
                    # We don't have any waiting line for this file, so proceed
                    zeek_line = file_handler.readline()
                    # self.print(f'Reading from file {filename}, the line {zeek_line}', 0, 6)
                    # Did the file ended?
                    if not zeek_line:
                        # We reached the end of one of the files that we were reading. Wait for more data to come
                        continue

                    # Since we actually read something form any file, update the last time of read
                    last_updated_file_time = datetime.now()
                    try:
                        # Convert from json to dict
                        nline = json.loads(zeek_line)
                        line = {}
                        # All bro files have a field 'ts' with the timestamp.
                        # So we are safe here not checking the type of line
                        try:
                            timestamp = nline['ts']
                        except KeyError:
                            # In some Zeek files there may not be a ts field
                            # Like in some weird smb files
                            timestamp = 0
                        # Add the type of file to the dict so later we know how to parse it
                        line['type'] = filename
                        line['data'] = nline
                    except json.decoder.JSONDecodeError:
                        # It is not JSON format. It is tab format line.
                        nline = zeek_line
                        # Ignore comments at the beginning of the file.
                        try:
                            if nline[0] == '#':
                                continue
                        except IndexError:
                            continue
                        line = {}
                        line['type'] = filename
                        line['data'] = nline
                        # Get timestamp
                        timestamp = nline.split('\t')[0]

                    time_last_lines[filename] = timestamp

                    # self.print(f'File {filename}. TS: {timestamp}')
                    # Store the line in the cache
                    # self.print(f'Adding cache and time of {filename}')
                    cache_lines[filename] = line

            ################
            # Out of the for that check each Zeek file one by one
            # self.print('Out of the for.')
            # self.print('Cached lines: {}'.format(str(cache_lines)))

            # If we don't have any cached lines to send, it may mean that new lines are not arriving. Check
            if not cache_lines:
                # Verify that we didn't have any new lines in the last 10 seconds. Seems enough for any network to have ANY traffic
                now = datetime.now()
                diff = now - last_updated_file_time
                diff = diff.seconds
                if diff >= self.bro_timeout:
                    # It has been 10 seconds without any file being updated. So stop the while
                    # Get out of the while and stop Zeek
                    break

            # Now read lines in order. The line with the smallest timestamp first
            file_sorted_time_last_lines = sorted(time_last_lines, key=time_last_lines.get)
            # self.print('Sorted times: {}'.format(str(file_sorted_time_last_lines)))
            try:
                key = file_sorted_time_last_lines[0]
            except IndexError:
                # No more sorted keys. Just loop waiting for more lines
                # It may happened that we check all the files in the folder, and there is still no file for us.
                # To cover this case, just refresh the list of files
                # self.print('Getting new files...')
                # print(cache_lines)
                zeek_files = __database__.get_all_zeek_file()
                time.sleep(1)
                continue

            # Description??
            line_to_send = cache_lines[key]
            # self.print('Line to send from file {}. {}'.format(key, line_to_send))
            # SENT
            self.print("	> Sent Line: {}".format(line_to_send), 0, 3)
            self.profilerqueue.put(line_to_send)
            # Count the read lines
            lines += 1
            # Delete this line from the cache and the time list
            # self.print('Deleting cache and time of {}'.format(key))
            del cache_lines[key]
            del time_last_lines[key]

            # Get the new list of files. Since new files may have been created by Zeek while we were processing them.
            zeek_files = __database__.get_all_zeek_file()

        ################
        # Out of the while

        # We reach here after the break produced if no zeek files are being updated.
        # No more files to read. Close the files
        for file in open_file_handlers:
            self.print('Closing file {}'.format(file), 3, 0)
            open_file_handlers[file].close()
        return lines

    def run(self):
        try:
            # Process the file that was given
            lines = 0
            if self.input_type == 'file':
                """
                Path to the flow input file to read. It can be a Argus
                binetflow flow, a Zeek conn.log file or a Zeek folder
                with all the log files.
                """

                # If the type of file is 'file (-f) and the name of the file is '-' then read from stdin
                if not self.input_information or self.input_information == '-':
                    self.print('Receiving flows from the stdin.', 3, 0)
                    # By default read the stdin
                    sys.stdin.close()
                    sys.stdin = os.fdopen(0, 'r')
                    file_stream = sys.stdin
                    line = {}
                    line['type'] = 'stdin'
                    for t_line in file_stream:
                        line['data'] = t_line
                        self.print(f'	> Sent Line: {t_line}', 0, 3)
                        self.profilerqueue.put(line)
                        lines += 1

                elif self.input_information:
                    # Are we given a file or a folder?
                    if os.path.isdir(self.input_information):
                        # This is the case that a folder full of zeek files is passed with -f. Read them all
                        for file in os.listdir(self.input_information):
                            # Remove .log extension and add file name to database.
                            extension = file[-4:]
                            if extension == '.log':
                                # Add log file to database
                                file_name_without_extension = file[:-4]
                                __database__.add_zeek_file(self.input_information + '/' + file_name_without_extension)

                        # We want to stop bro if no new line is coming.
                        self.bro_timeout = 1
                        lines = self.read_zeek_files()
                        self.print("We read everything from the folder. No more input. Stopping input process. Sent {} lines".format(lines))
                    else:
                        # Is a file. Read and send independently of the type
                        # of input
                        self.print(f'Receiving flows from the single file {self.input_information}', 3, 0)

                        # Try read a unique Zeek file
                        file_stream = open(self.input_information)
                        line = {}
                        headers_line = self.input_information.split('/')[-1]
                        if 'binetflow' in headers_line or 'argus' in headers_line:
                            line['type'] = 'argus'
                            # fake = {'type': 'argus', 'data': 'StartTime,Dur,Proto,SrcAddr,Sport,Dir,DstAddr,Dport,State,sTos,dTos,TotPkts,TotBytes,SrcBytes,SrcPkts,Label\n'}
                            # self.profilerqueue.put(fake)
                            self.read_lines_delay = 0.02
                        elif 'log' in headers_line:
                            line['type'] = 'zeek'
                        for t_line in file_stream:
                            time.sleep(self.read_lines_delay)
                            line['data'] = t_line
                            self.print(f'	> Sent Line: {line}', 0, 3)
                            self.profilerqueue.put(line)
                            lines += 1
                        file_stream.close()

                self.profilerqueue.put("stop")
                self.outputqueue.put("02|input|[In] No more input. Stopping input process. Sent {} lines ({}).".format(lines, datetime.now().strftime('%Y-%m-%d--%H:%M:%S')))

                self.outputqueue.close()
                self.profilerqueue.close()

                return True
            # Process the binary nfdump file.
            elif self.input_type == 'nfdump':
                # Its not good to read the nfdump file to disk.
                command = 'nfdump -b -N -o csv -q -r ' + self.input_information + ' >  ' + self.nfdump_output_file
                os.system(command)
                self.nfdump_timeout = 10
                lines = self.read_nfdump_file()
                self.print("We read everything. No more input. Stopping input process. Sent {} lines".format(lines))
                # Delete the nfdump file
                command = "rm " + self.nfdump_output_file + "2>&1 > /dev/null &"
                os.system(command)

            # Process the pcap files
            elif self.input_type == 'pcap' or self.input_type == 'interface':
                # Create zeek_folder if does not exist.
                if not os.path.exists(self.zeek_folder):
                    os.makedirs(self.zeek_folder)
                # Now start the observer of new files. We need the observer because Zeek does not create all the files
                # at once, but when the traffic appears. That means that we need
                # some process to tell us which files to read in real time when they appear
                # Get the file eventhandler
                # We have to set event_handler and event_observer before running zeek.
                self.event_handler = FileEventHandler(self.config)
                # Create an observer
                self.event_observer = Observer()
                # Schedule the observer with the callback on the file handler
                self.event_observer.schedule(self.event_handler, self.zeek_folder, recursive=True)
                # Start the observer
                self.event_observer.start()

                # This double if is horrible but we just need to change a string
                if self.input_type == 'interface':
                    # Change the bro command
                    bro_parameter = '-i ' + self.input_information
                    # We don't want to stop bro if we read from an interface
                    self.bro_timeout = 9999999999999999
                elif self.input_type == 'pcap':
                    # We change the bro command
                    bro_parameter = '-r'
                    # Find if the pcap file name was absolute or relative
                    if self.input_information[0] == '/':
                        # If absolute, do nothing
                        bro_parameter = '-r "' + self.input_information  + '"'
                    else:
                        # If relative, add ../ since we will move into a special folder
                        bro_parameter = '-r "../' + self.input_information + '"'
                    # This is for stoping the input if bro does not receive any new line while reading a pcap
                    self.bro_timeout = 30

                if len(os.listdir(self.zeek_folder)) > 0:
                    # First clear the zeek folder of old .log files
                    # The rm should not be in background because we must wait until the folder is empty
                    command = "rm " + self.zeek_folder + "/*.log 2>&1 > /dev/null"
                    os.system(command)

                # Run zeek on the pcap or interface. The redef is to have json files
                # To add later the home net: "Site::local_nets += { 1.2.3.0/24, 5.6.7.0/24 }"
                command ="cd " + self.zeek_folder + "; " + self.zeek_or_bro + " -C " + bro_parameter + "  " + self.tcp_inactivity_timeout + " local -e 'redef LogAscii::use_json=T;' -f " + self.packet_filter + " 2>&1 > /dev/null &"
                self.print(f'Zeek command: {command}', 3, 0)
                # Run zeek.
                os.system(command)

                # Give Zeek some time to generate at least 1 file.
                time.sleep(3)

                lines = self.read_zeek_files()
                self.print("We read everything. No more input. Stopping input process. Sent {} lines".format(lines))

                # Stop the observer
                try:
                    self.event_observer.stop()
                    self.event_observer.join()
                except AttributeError:
                    # In the case of nfdump, there is no observer
                    pass
                return True

        except KeyboardInterrupt:
            self.outputqueue.put("04|input|[In] No more input. Stopping input process. Sent {} lines".format(lines))
            try:
                self.event_observer.stop()
                self.event_observer.join()
            except AttributeError:
                # In the case of nfdump, there is no observer
                pass
            except NameError:
                pass
            return True
        except Exception as inst:
            self.print("Problem with Input Process.", 0, 1)
            self.print("Stopping input process. Sent {} lines".format(lines), 0, 1)
            self.print(type(inst), 0, 1)
            self.print(inst.args, 0, 1)
            self.print(inst, 0, 1)
            try:
                self.event_observer.stop()
                self.event_observer.join()
            except AttributeError:
                # In the case of nfdump, there is no observer
                pass
            self.print(traceback.format_exc())
            sys.exit(1)
