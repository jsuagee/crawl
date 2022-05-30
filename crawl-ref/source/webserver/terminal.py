import pty
import termios
import os
import fcntl
import struct
import resource
import signal
import sys
import time

BUFSIZ = 2048

class TerminalRecorder(object):
    def __init__(self, command, filename, id_header, logger, io_loop, termsize):
        self.io_loop = io_loop
        self.command = command
        if filename:
            self.ttyrec = open(filename, "w", 0)
        else:
            self.ttyrec = None
        self.id = id
        self.returncode = None
        self.output_buffer = ""
        self.termsize = termsize

        self.pid = None
        self.child_fd = None

        self.end_callback = None
        self.output_callback = None
        self.activity_callback = None
        self.error_callback = None

        self.errpipe_read = None
        self.error_buffer = ""

        self.logger = logger

        if id_header:
            self.write_ttyrec_chunk(id_header)

        self._spawn()

    def _spawn(self):
        self.errpipe_read, errpipe_write = os.pipe()

        self.pid, self.child_fd = pty.fork()

        # Remember that from here on there are two parallel copies of
        # this function running. One of them is the original and the other is
        # the fork. So in the next line we need to filter what we do based
        # on whether we're the original process or the fork:
        if self.pid == 0:
            # We're the child
            def handle_signal(signal, f):
                sys.exit(0)
            signal.signal(1, handle_signal)

            # Set window size
            cols, lines = self.get_terminal_size()
            s = struct.pack("HHHH", lines, cols, 0, 0)
            fcntl.ioctl(sys.stdout.fileno(), termios.TIOCSWINSZ, s)

            os.close(self.errpipe_read)
            os.dup2(errpipe_write, 2)

            # Make sure not to retain any files from the parent
            max_fd = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
            for i in range(3, max_fd):
                try:
                    os.close(i)
                except OSError:
                    pass

            # And exec
            env            = dict(os.environ)
            env["COLUMNS"] = str(cols)
            env["LINES"]   = str(lines)
            env["TERM"]    = "linux"
            try:
                os.execvpe(self.command[0], self.command, env)
            except OSError:
                sys.exit(1)

        # We're the parent
        os.close(errpipe_write)

        # JTS
        # What class is the io_loop? It's a tornado IOLoop object
        self.io_loop.add_handler(self.child_fd,
                                 self._handle_read,
                                 self.io_loop.ERROR | self.io_loop.READ)

        self.io_loop.add_handler(self.errpipe_read,
                                 self._handle_err_read,
                                 self.io_loop.READ)

    def _handle_read(self, fd, events):
        # JTS
        # What is being read here is from the file descriptor of the
        # terminal program crawl. That is output from the crawl program.
        if events & self.io_loop.READ:
            buf = os.read(fd, BUFSIZ)

            if len(buf) > 0:
                self.write_ttyrec_chunk(buf)

                if self.activity_callback:
                    self.activity_callback()

                self.output_buffer += buf
                self._do_output_callback()

            self.poll()

        if events & self.io_loop.ERROR:
            self.poll()

    def _handle_err_read(self, fd, events):
        if events & self.io_loop.READ:
            buf = os.read(fd, BUFSIZ)

            if len(buf) > 0:
                self.error_buffer += buf
                self._log_error_output()

            self.poll()

    def write_ttyrec_header(self, sec, usec, l):
        if self.ttyrec is None: return
        s = struct.pack("<iii", sec, usec, l)
        self.ttyrec.write(s)

    def write_ttyrec_chunk(self, data):
        if self.ttyrec is None: return
        t = time.time()
        self.write_ttyrec_header(int(t), int((t % 1) * 1000000), len(data))
        self.ttyrec.write(data)

    def _do_output_callback(self):
        # JTS
        # Gets position of the end of the first line in the buffer
        pos = self.output_buffer.find("\n")
        while pos >= 0:
            line = self.output_buffer[:pos]
            self.output_buffer = self.output_buffer[pos + 1:]

            if len(line) > 0:
                if line[-1] == "\r": line = line[:-1]

                if self.output_callback:
                    # JTS
                    # This would be the _on_process_output() method in the
                    # CrawlProcessHandler object, which ultimately calls the
                    # write_message() method of the CrawlWebSocket object.
                    # This goes back to the player's browser.
                    self.output_callback(line)

            pos = self.output_buffer.find("\n")

    def _log_error_output(self):
        pos = self.error_buffer.find("\n")
        while pos >= 0:
            line = self.error_buffer[:pos]
            self.error_buffer = self.error_buffer[pos + 1:]

            if len(line) > 0:
                if line[-1] == "\r": line = line[:-1]

                self.logger.info("ERR: %s", line)
                if self.error_callback:
                    self.error_callback(line)

            pos = self.error_buffer.find("\n")


    def send_signal(self, signal):
        os.kill(self.pid, signal)

    def poll(self):
        if self.returncode is None:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid == self.pid:
                if os.WIFSIGNALED(status):
                    self.returncode = -os.WTERMSIG(status)
                elif os.WIFEXITED(status):
                    self.returncode = os.WEXITSTATUS(status)
                else:
                    # Should never happen
                    raise RuntimeError("Unknown child exit status!")

            if self.returncode is not None:
                self.io_loop.remove_handler(self.child_fd)
                self.io_loop.remove_handler(self.errpipe_read)

                os.close(self.child_fd)
                os.close(self.errpipe_read)

                if self.ttyrec:
                    self.ttyrec.close()

                if self.end_callback:
                    self.end_callback()

        return self.returncode

    def get_terminal_size(self):
        return self.termsize

    def write_input(self, data):
        # JTS
        # self.poll() is polling to see that the process is still ongoing
        # and hasn't terminated. If it is still ongoing self.poll() will
        # return None.
        if self.poll() is not None: return

        while len(data) > 0:
            # JTS
            # self.child_fd is the file descriptor for the child process
            # forked up in the _spawn() function, and is supposedly now in
            # the middle of running the crawl program using
            # os.execvpe()
            # So this is writing the user's input commands directly into
            # that terminal program.

            written = os.write(self.child_fd, data)
            data = data[written:]
