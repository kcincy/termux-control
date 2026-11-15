"""Give the tmux client a controlling TTY, without preexec_fn in the ASGI process."""
import fcntl
import os
import sys
import termios

if __name__ == '__main__':
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    executable, socket, session = sys.argv[1:]
    os.execv(executable, [executable, '-S', socket, 'attach-session', '-t', session])
