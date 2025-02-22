# SPDX-License-Identifier: BSD-3-Clause
# Copyright(c) 2020 Ericsson AB

from collections import deque
from enum import Enum
import errno
import json
import random
import select
import time
import os

import paf.xcm as xcm
import paf.proto as proto

MATCH_TYPE_APPEARED = proto.MATCH_TYPE_APPEARED
MATCH_TYPE_MODIFIED = proto.MATCH_TYPE_MODIFIED
MATCH_TYPE_DISAPPEARED = proto.MATCH_TYPE_DISAPPEARED

MAX_MSGS_PER_ROUND = 128

ProtocolError = proto.ProtocolError
TransportError = proto.TransportError
Error = proto.Error


class TransactionError(Error):
    def __init__(self, reason=None):
        if reason is not None:
            message = "Protocol transaction failed: '%s'." % reason
        else:
            message = "Protocol transaction failed for unknown reason." % \
                reason
        Error.__init__(self, message)


class EventType(Enum):
    ACCEPT = 0
    NOTIFY = 1
    COMPLETE = 2
    FAIL = 3


class TransactionState(Enum):
    IDLE = 0
    REQUESTING = 1
    ACCEPTED = 2
    TERMINATED = 3


class Transaction:
    def __init__(self, ta_type, ta_id):
        self.ta_id = ta_id
        self.ta_type = ta_type
        self.state = TransactionState.IDLE

    def produce_request(self, request_args, request_optargs,
                        response_cb):
        request_msg = {}

        proto.FIELD_TA_CMD.put(self.ta_type.cmd, request_msg)
        proto.FIELD_TA_ID.put(self.ta_id, request_msg)
        proto.FIELD_MSG_TYPE.put(proto.MSG_TYPE_REQUEST, request_msg)

        assert len(request_args) == len(self.ta_type.request_fields)

        for i, field in enumerate(self.ta_type.request_fields):
            field_value = request_args[i]
            field.put(field_value, request_msg)

        for opt_field in self.ta_type.opt_request_fields:
            if opt_field.name in request_optargs:
                field_value = request_optargs.get(opt_field.python_name())
                if field_value is not None:
                    opt_field.put(field_value, request_msg)
                del request_optargs[opt_field.name]

        assert len(request_optargs) == 0

        self.cb = response_cb
        self.state = TransactionState.REQUESTING
        return request_msg

    def consume_message(self, in_msg):
        ta_cmd = proto.FIELD_TA_CMD.pull(in_msg)
        if ta_cmd != self.ta_type.cmd:
            raise ProtocolError("Received message in transaction %d; expected "
                                "\"%s\" command, but got \"%s\"." %
                                (self.ta_id, self.ta_type.cmd, ta_cmd))

        msg_type = proto.FIELD_MSG_TYPE.pull(in_msg)

        if msg_type == proto.MSG_TYPE_ACCEPT and \
           self.state == TransactionState.REQUESTING and \
           self.ta_type.ia_type == proto.InteractionType.MULTI_RESPONSE:
            event = EventType.ACCEPT
            fields = self.ta_type.accept_fields
            opt_fields = self.ta_type.opt_accept_fields
            self.state = TransactionState.ACCEPTED
        elif (msg_type == proto.MSG_TYPE_NOTIFY and
              self.state == TransactionState.ACCEPTED):
            event = EventType.NOTIFY
            fields = self.ta_type.notify_fields
            opt_fields = self.ta_type.opt_notify_fields
        elif (msg_type == proto.MSG_TYPE_COMPLETE and
              ((self.state == TransactionState.REQUESTING and
                self.ta_type.ia_type == proto.InteractionType.SINGLE_RESPONSE)
               or
               (self.state == TransactionState.ACCEPTED and
                self.ta_type.ia_type ==
                proto.InteractionType.MULTI_RESPONSE))):
            fields = self.ta_type.complete_fields
            opt_fields = self.ta_type.opt_complete_fields
            event = EventType.COMPLETE
            self.state = TransactionState.TERMINATED
        elif msg_type == proto.MSG_TYPE_FAIL:
            fields = self.ta_type.fail_fields
            opt_fields = self.ta_type.opt_fail_fields
            event = EventType.FAIL
            self.state = TransactionState.TERMINATED
        else:
            raise ProtocolError("Received invalid message type %s "
                                "for %s transaction %d in state %s" %
                                (msg_type, self.ta_type.cmd,
                                 self.ta_id, self.state.name))
        args = [field.pull(in_msg) for field in fields]

        optargs = {}
        for opt_field in opt_fields:
            opt_value = opt_field.pull(in_msg, opt=True)
            if opt_value is not None:
                optargs[opt_field.python_name()] = opt_value

        if len(in_msg) > 0:
            raise ProtocolError("Server sent message with unknown fields: "
                                "%s" % list(in_msg.keys()))
        self.cb(self.ta_id, event, *args, **optargs)


def wait(conn, criteria):
    poll = select.poll()
    poll.register(conn.fileno(), select.POLLIN)
    while not criteria():
        poll.poll()
        conn.process()


class Call:
    def __init__(self, conn):
        self.conn = conn
        self.ta_id = None
        self.result = None

    def __call__(self, ta_id, event, *args, **optargs):
        assert self.ta_id is not None
        assert self.ta_id == ta_id
        self.result = event
        if self.result == EventType.FAIL:
            self.reason = optargs.get('fail_reason')

    def get(self):
        wait(self.conn, lambda: self.result == EventType.COMPLETE or
             self.result == EventType.FAIL)
        if self.result == EventType.FAIL:
            raise TransactionError(reason=self.reason)


class LatencyCall(Call):
    def __init__(self, conn):
        self.start = time.time()
        Call.__init__(self, conn)

    def __call__(self, ta_id, event, *args):
        if event == EventType.COMPLETE:
            self.latency = time.time() - self.start
        Call.__call__(self, ta_id, event, *args)

    def get(self):
        Call.get(self)
        return self.latency


class NotifyCall(Call):
    def __init__(self, conn):
        Call.__init__(self, conn)
        self.notifications = []

    def __call__(self, ta_id, event, *args, **optargs):
        if event == EventType.NOTIFY:
            notification = list(args)
            if len(optargs) > 0:
                notification.append(optargs)
            self.notifications.append(notification)
        Call.__call__(self, ta_id, event, *args, **optargs)

    def get(self):
        Call.get(self)
        return self.notifications


class CompleteCall(Call):
    def __init__(self, conn):
        Call.__init__(self, conn)

    def __call__(self, ta_id, event, *args):
        if event == EventType.COMPLETE:
            self.complete = args
        Call.__call__(self, ta_id, event, *args)

    def get(self):
        Call.get(self)
        return self.complete


class Client:
    def __init__(self, client_id, addr, ready_cb):
        self.client_id = client_id
        try:
            self.conn_sock = xcm.connect(addr, xcm.NONBLOCK)
        except xcm.error as e:
            raise TransportError(str(e))
        self.ready_cb = ready_cb
        self.ta_id = 0
        self.out_wire_msgs = deque()
        self.transactions = {}
        self.proto_version = None
        self.update()
        try:
            self.initial_hello()
        except Error:
            self.close()
            raise

    def initial_hello(self):
        self.hello(self.initial_hello_cb)
        if self.ready_cb is None:
            wait(self, criteria=lambda: self.proto_version is not None)

    def initial_hello_cb(self, ta_id, event, *args, **optargs):
        if event == EventType.FAIL:
            reason = optargs.get('fail_reason')
            if reason is None:
                reason = "reason unknown"
            raise ProtocolError("Protocol establishment failed: %s" %
                                reason)
        elif event == EventType.COMPLETE:
            selected_version = args[0]
            if proto.VERSION != selected_version:
                raise ProtocolError("Server selected unsupported "
                                    "protocol version %d (required %d)"
                                    % (selected_version, proto.VERSION))
            self.proto_version = selected_version
            if self.ready_cb is not None:
                self.ready_cb()

    def close(self):
        self.conn_sock.close()

    def hello(self, response_cb=None):
        return self.issue_request(proto.TA_HELLO,
                                  (self.client_id, proto.VERSION,
                                   proto.VERSION), {},
                                  CompleteCall, response_cb)

    def publish(self, service_id, generation, service_props, ttl,
                response_cb=None):
        return self.issue_request(proto.TA_PUBLISH, (service_id, generation,
                                                     service_props, ttl),
                                  {}, Call, response_cb)

    def unpublish(self, service_id, response_cb=None):
        return self.issue_request(proto.TA_UNPUBLISH, (service_id,),
                                  {}, Call, response_cb)

    def subscribe(self, sub_id, response_cb, filter=None):
        return self.async_request(proto.TA_SUBSCRIBE, (sub_id,),
                                  {'filter': filter}, response_cb)

    def unsubscribe(self, sub_id, response_cb=None):
        return self.issue_request(proto.TA_UNSUBSCRIBE, (sub_id,), {},
                                  Call, response_cb)

    def subscriptions(self, response_cb=None):
        return self.issue_request(proto.TA_SUBSCRIPTIONS, (), {},
                                  NotifyCall, response_cb)

    def services(self, response_cb=None, filter=None):
        return self.issue_request(proto.TA_SERVICES, (), {'filter': filter},
                                  NotifyCall, response_cb)

    def ping(self, response_cb=None):
        return self.issue_request(proto.TA_PING, (), {}, LatencyCall,
                                  response_cb)

    def clients(self, response_cb=None):
        return self.issue_request(proto.TA_CLIENTS, (), {}, NotifyCall,
                                  response_cb)

    def service_id(self):
        return self.gen_id()

    def subscription_id(self):
        return self.gen_id()

    def gen_id(self):
        INT64_MAX = 0x7fffffffffffffff
        return random.randint(0, INT64_MAX)

    def next_ta_id(self):
        ta_id = self.ta_id
        self.ta_id += 1
        return ta_id

    def issue_request(self, ta_type, request_args, request_optargs,
                      call_cls, response_cb):
        if response_cb is not None:
            return self.async_request(ta_type, request_args, request_optargs,
                                      response_cb)
        else:
            assert call_cls is not None
            return self.sync_request(ta_type, request_args,
                                     request_optargs, call_cls)

    def sync_request(self, ta_type, request_args, request_optargs,
                     call_cls):
        call = call_cls(self)
        ta_id = self.async_request(ta_type, request_args, request_optargs,
                                   call)
        assert ta_id is not None
        call.ta_id = ta_id
        return call.get()

    def async_request(self, ta_type, request_args, request_optargs,
                      response_cb):
        ta_id = self.next_ta_id()
        transaction = Transaction(ta_type, ta_id)
        request_msg = transaction.produce_request(request_args,
                                                  request_optargs,
                                                  response_cb)
        out_wire_msg = json.dumps(request_msg).encode('utf-8')
        self.out_wire_msgs.append(out_wire_msg)
        self.transactions[ta_id] = transaction
        self.try_send()
        return ta_id

    def fileno(self):
        return self.conn_sock.fileno()

    def update(self):
        condition = xcm.SO_RECEIVABLE
        if len(self.out_wire_msgs) > 0:
            condition |= xcm.SO_SENDABLE
        self.conn_sock.update(condition)

    def process(self):
        for i in range(0, MAX_MSGS_PER_ROUND):
            if not self.try_send():
                break
        for i in range(0, MAX_MSGS_PER_ROUND):
            if not self.try_receive():
                break

    def try_send(self):
        try:
            if len(self.out_wire_msgs) > 0:
                out_wire_msg = self.out_wire_msgs.popleft()
                self.conn_sock.send(out_wire_msg)
                return True
            else:
                return False
        except xcm.error as e:
            if e.errno == errno.EAGAIN:
                self.out_wire_msgs.appendleft(out_wire_msg)
                return False
            else:
                raise TransportError(str(e))
        finally:
            self.update()

    def try_receive(self):
        try:
            in_wire_msg = self.conn_sock.receive()
            if len(in_wire_msg) == 0:
                raise ProtocolError("Server closed connection")
            in_msg = json.loads(in_wire_msg.decode('utf-8'))
            ta_id = proto.FIELD_TA_ID.pull(in_msg)
            if ta_id not in self.transactions:
                raise ProtocolError("Received message related to unknown "
                                    "transaction %d" % ta_id)
            transaction = self.transactions[ta_id]
            transaction.consume_message(in_msg)
            if transaction.state == TransactionState.TERMINATED:
                del self.transactions[ta_id]
            return True
        except xcm.error as e:
            if e.errno == errno.EAGAIN:
                return False
            else:
                raise TransportError(str(e))
        except ValueError:
            raise ProtocolError("Error decoding response message JSON")
        finally:
            self.update()


DOMAINS_ENV = 'PAF_DOMAINS'
DEFAULT_DOMAINS_DIR = '/run/paf/domains.d'


def domain_addrs(domain):
    domains_dir = DEFAULT_DOMAINS_DIR
    if DOMAINS_ENV in os.environ:
        domains_dir = os.environ[DOMAINS_ENV]
    domains_file = "%s/%s" % (domains_dir, domain)
    try:
        addrs = []
        for line in open(domains_file):
            addr = line.strip()
            if addr[0] == '#':
                continue
            addrs.append(addr)
        return addrs
    except IOError:
        return []


def domain_addr(domain):
    addrs = domain_addrs(domain)
    if len(addrs) > 0:
        return addrs[0]
    else:
        return None


def allocate_client_id():
    return random.randint(0, ((1 << 63) - 1))


def connect(domain_or_addr, client_id=None, ready_cb=None):
    addr = domain_addr(domain_or_addr)
    if addr is None:
        addr = domain_or_addr
    if client_id is None:
        client_id = allocate_client_id()
    return Client(client_id, addr, ready_cb)
