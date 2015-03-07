#!/usr/bin/env python3

import os
import os.path
import sys
import configparser
import logging
import smtplib
from argparse import ArgumentParser, FileType
from subprocess import call, check_call, check_output, CalledProcessError
from fnmatch import fnmatch

import pyinotify
from tornado.ioloop import IOLoop
from tornado import gen
import tornado.process

import toml

from nicelogger import enable_pretty_logging
enable_pretty_logging(logging.DEBUG)
logger = logging.getLogger(__name__)

events = pyinotify.IN_CLOSE_WRITE | pyinotify.IN_MOVED_TO
REPORTMAIL = 'farseerfc@archlinuxcn.org'
MYADDRESS = '<filesink@build.archlinuxcn.org>'
MYNAME = 'filesink'
MYEMAIL = MYNAME + ' ' + MYADDRESS


class FakeEvent():
    def __init__(self, pathname):
        self.pathname = pathname
        self.name = os.path.basename(pathname)


class EventHandler(pyinotify.ProcessEvent):
    def my_init(self, wm, ioloop, config):
        self.ioloop = ioloop
        self.wm = wm
        self.__dict__.update(config)

        if wm is not None:
            wm.add_watch(self.watch, events)
            logger.info('Watching \"%s\" to \"%s\"', self.watch, self.target)

    def process_IN_CLOSE_WRITE(self, event):
        logger.info('IN_CLOSE_WRITE: %s', event.pathname)
        self.handle(event)

    def process_IN_MOVED_TO(self, event):
        logger.info('IN_MOVED_TO: %s', event.pathname)
        self.handle(event)

    def local_sum(self, event):
        logger.debug("Execute: \"%s %s\"", self.sumcmd, event.pathname)
        output = check_output([self.sumcmd, event.pathname]).decode("utf-8")
        return output.split('\n')[0].split(' ')[0]

    def copy(self, event):
        target_path = "%s:%s" % (self.machine, self.target)
        cmdline = "%s %s %s" % (self.cpcmd,
                                os.path.join(self.watch, event.name),
                                target_path)
        logger.debug("Execute: \"%s\"", cmdline)
        check_call(cmdline, shell=True)

    def remote_sum(self, event, md5source):
        target_file = os.path.join(self.target, event.name)
        cmd = "echo '%s  %s' | ssh %s %s -c -" % (md5source, target_file, self.machine, self.sumcmd)
        logger.debug("Execute: \"%s\"", cmd)
        return call(cmd, shell=True)

    def delete_local(self, event):
        logger.info("Delete: \"%s\"", event.pathname)
        os.remove(event.pathname)

    def process(self, event, time):
        logger.debug("Moving for %d time: %s", time + 1, event.pathname)
        md5source = self.local_sum(event)
        self.copy(event)
        if self.remote_sum(event, md5source) == 0:
            self.delete_local(event)
            return True
        else:
            logger.warning("Sum failed, not deleted: %s", event.pathname)
            return False

    def handle(self, event):
        if not fnmatch(event.name, self.pattern):
            return
        succeed = False
        errorobj = ""
        for time in range(self.retries):
            try:
                if self.process(event, time):
                    succeed = True
                    break
                logger.warning("Failed for %d time: %s",
                               time + 1, event.pathname)
            except CalledProcessError as e:
                logger.error("Error %s: %s", event.pathname, e)
                errorobj = e
        if not succeed:
            sendmail(REPORTMAIL, MYEMAIL,
                     "[filesink] Move File Failed",
                     "\n".join([
                         "Failed to move file \"%s\"" % event.pathname,
                         "From path \"%s\"" % self.watch,
                         "To machine \"%s\"" % self.machine,
                         "Path \"%s\"" % self.target,
                         "Error: %s" % errorobj
                         ]))

    def oneshot(self):
        for path in os.listdir(self.watch):
            fullpath = os.path.join(self.watch, path)
            if os.path.isfile(fullpath):
                logger.info("Faking: \"%s\"", fullpath)
                self.handle(FakeEvent(fullpath))


def sendmail(to, from_, subject, msg):
    try:
        s = smtplib.SMTP()
        s.connect()
        msg = assemble_mail(subject, to, from_, text=msg)
        s.send_message(msg)
        s.quit()
    except:
        pass


def sinkmon(config):
    wm = pyinotify.WatchManager()
    ioloop = IOLoop.instance()

    handler = EventHandler(
        config=config,
        wm=wm,
        ioloop=ioloop,
    )
    return pyinotify.TornadoAsyncNotifier(
        wm,
        ioloop,
        default_proc_fun=handler,
    )


def main(prog, args):
    config = toml.loads(args.config.read())
    default_config = dict((c, config[c])
                          for c in config
                          if type(config[c]) is not dict)
    watches = [dict(list(default_config.items())
                    + list(config[c].items())
                    # specific config will override default config
                    + [('name', c)])
               for c in config if type(config[c]) is dict]

    if args.once:
        for w in watches:
            handler = EventHandler(
                config=w,
                wm=None,
                ioloop=None,
            )
            handler.oneshot()
        sys.exit(0)

    notifiers = [sinkmon(w) for w in watches]
    ioloop = IOLoop.instance()
    logger.info('Starting %s', prog)
    try:
        ioloop.start()
    except KeyboardInterrupt:
        logger.info('Stoping %s', prog)
        for notifier in notifiers:
            notifier.stop()
        ioloop.close()

if __name__ == '__main__':
    parser = ArgumentParser(
        description="Move files over network.")
    parser.add_argument('--once',
                        help='run once and return',
                        action="store_true")
    parser.add_argument('config',
                        type=FileType('r'),
                        help='config file in toml format')
    args = parser.parse_args()
    main(sys.argv[0], args)
