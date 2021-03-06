#!/usr/bin/env python
# coding: utf-8

import os
import subprocess
import tempfile
import testgres
import time
import unittest

import logging.config

from distutils.version import LooseVersion

from testgres import \
    InitNodeException, \
    StartNodeException, \
    ExecUtilException, \
    BackupException, \
    QueryException, \
    CatchUpException, \
    TimeoutException

from testgres import \
    TestgresConfig, \
    configure_testgres

from testgres import \
    NodeStatus, \
    IsolationLevel, \
    get_new_node

from testgres import \
    get_bin_path, \
    get_pg_config

from testgres import bound_ports


def util_is_executable(util):
    exe_file = get_bin_path(util)

    # check if util exists
    if os.path.exists(exe_file):
        return True

    # check if util is in PATH
    for path in os.environ["PATH"].split(os.pathsep):
        exe_file = os.path.join(path, util)
        if os.path.exists(exe_file):
            return True


class SimpleTest(unittest.TestCase):
    def test_custom_init(self):
        with get_new_node('test') as node:
            # enable page checksums
            node.init(initdb_params=['-k']).start()
            node.safe_psql('postgres', 'select 1')

        with get_new_node('test') as node:
            node.init(
                allow_streaming=True,
                initdb_params=['--auth-local=reject', '--auth-host=reject'])

            hba_file = os.path.join(node.data_dir, 'pg_hba.conf')
            with open(hba_file, 'r') as conf:
                lines = conf.readlines()

                # check number of lines
                self.assertGreaterEqual(len(lines), 6)

                # there should be no trust entries at all
                self.assertFalse(any('trust' in s for s in lines))

    def test_double_init(self):
        with get_new_node('test') as node:
            node.init()

            # can't initialize node more than once
            with self.assertRaises(InitNodeException):
                node.init()

    def test_init_after_cleanup(self):
        with get_new_node('test') as node:
            node.init().start()
            node.status()
            node.safe_psql('postgres', 'select 1')

            node.cleanup()

            node.init().start()
            node.status()
            node.safe_psql('postgres', 'select 1')

    def test_double_start(self):
        with get_new_node('test') as node:
            node.init().start()

            # can't start node more than once
            with self.assertRaises(StartNodeException):
                node.start()

    def test_uninitialized_start(self):
        with get_new_node('test') as node:
            # node is not initialized yet
            with self.assertRaises(StartNodeException):
                node.start()

    def test_restart(self):
        with get_new_node('test') as node:
            node.init().start()

            # restart, ok
            res = node.execute('postgres', 'select 1')
            self.assertEqual(res, [(1, )])
            node.restart()
            res = node.execute('postgres', 'select 2')
            self.assertEqual(res, [(2, )])

            # restart, fail
            with self.assertRaises(StartNodeException):
                node.append_conf('pg_hba.conf', 'DUMMY')
                node.restart()

    def test_psql(self):
        with get_new_node('test') as node:
            node.init().start()

            # check default params
            with self.assertRaises(QueryException):
                node.psql('postgres')

            # check returned values
            res = node.psql('postgres', 'select 1')
            self.assertEqual(res[0], 0)
            self.assertEqual(res[1], b'1\n')
            self.assertEqual(res[2], b'')

            # check returned values
            res = node.safe_psql('postgres', 'select 1')
            self.assertEqual(res, b'1\n')

            # check feeding input
            node.safe_psql('postgres', 'create table horns (w int)')
            node.safe_psql(
                'postgres',
                'copy horns from stdin (format csv)',
                input=b"1\n2\n3\n\.\n")
            sum = node.safe_psql('postgres', 'select sum(w) from horns')
            self.assertEqual(sum, b'6\n')
            node.safe_psql('postgres', 'drop table horns')

            node.stop()

            # check psql on stopped node
            with self.assertRaises(QueryException):
                node.safe_psql('postgres', 'select 1')

    def test_status(self):
        # check NodeStatus cast to bool
        self.assertTrue(NodeStatus.Running)

        # check NodeStatus cast to bool
        self.assertFalse(NodeStatus.Stopped)

        # check NodeStatus cast to bool
        self.assertFalse(NodeStatus.Uninitialized)

        # check statuses after each operation
        with get_new_node('test') as node:
            self.assertEqual(node.get_pid(), 0)
            self.assertEqual(node.status(), NodeStatus.Uninitialized)

            node.init()

            self.assertEqual(node.get_pid(), 0)
            self.assertEqual(node.status(), NodeStatus.Stopped)

            node.start()

            self.assertTrue(node.get_pid() > 0)
            self.assertEqual(node.status(), NodeStatus.Running)

            node.stop()

            self.assertEqual(node.get_pid(), 0)
            self.assertEqual(node.status(), NodeStatus.Stopped)

            node.cleanup()

            self.assertEqual(node.get_pid(), 0)
            self.assertEqual(node.status(), NodeStatus.Uninitialized)

    def test_simple_queries(self):
        with get_new_node('test') as node:
            node.init().start()

            res = node.psql('postgres', 'select 1')
            self.assertEqual(res, (0, b'1\n', b''))

            res = node.safe_psql('postgres', 'select 1')
            self.assertEqual(res, b'1\n')

            res = node.execute('postgres', 'select 1')
            self.assertListEqual(res, [(1, )])

            with node.connect('postgres') as con:
                res = con.execute('select 1')
                self.assertListEqual(res, [(1, )])

    def test_transactions(self):
        with get_new_node('test') as node:
            node.init().start()

            with node.connect('postgres') as con:
                con.begin()
                con.execute('create table test(val int)')
                con.execute('insert into test values (1)')
                con.commit()

                con.begin()
                con.execute('insert into test values (2)')
                res = con.execute('select * from test order by val asc')
                self.assertListEqual(res, [(1, ), (2, )])
                con.rollback()

                con.begin()
                res = con.execute('select * from test')
                self.assertListEqual(res, [(1, )])
                con.rollback()

                con.begin()
                con.execute('drop table test')
                con.commit()

    def test_control_data(self):
        with get_new_node('test') as node:

            # node is not initialized yet
            with self.assertRaises(ExecUtilException):
                node.get_control_data()

            node.init()
            data = node.get_control_data()

            # check returned dict
            self.assertIsNotNone(data)
            self.assertTrue(any('pg_control' in s for s in data.keys()))

    def test_backup_simple(self):
        with get_new_node('master') as master:

            # enable streaming for backups
            master.init(allow_streaming=True)

            # node must be running
            with self.assertRaises(BackupException):
                master.backup()

            # it's time to start node
            master.start()

            # fill node with some data
            master.psql('postgres',
                        'create table test as select generate_series(1, 4) i')

            with master.backup(xlog_method='stream') as backup:
                with backup.spawn_primary('slave') as slave:
                    slave.start()
                    res = slave.execute('postgres',
                                        'select * from test order by i asc')
                    self.assertListEqual(res, [(1, ), (2, ), (3, ), (4, )])

    def test_backup_multiple(self):
        with get_new_node('node') as node:
            node.init(allow_streaming=True).start()

            with node.backup(xlog_method='fetch') as backup1, \
                    node.backup(xlog_method='fetch') as backup2:

                self.assertNotEqual(backup1.base_dir, backup2.base_dir)

            with node.backup(xlog_method='fetch') as backup:
                with backup.spawn_primary('node1', destroy=False) as node1, \
                        backup.spawn_primary('node2', destroy=False) as node2:

                    self.assertNotEqual(node1.base_dir, node2.base_dir)

    def test_backup_exhaust(self):
        with get_new_node('node') as node:
            node.init(allow_streaming=True).start()

            with node.backup(xlog_method='fetch') as backup:

                # exhaust backup by creating new node
                with backup.spawn_primary('node1') as node1:    # noqa
                    pass

                # now let's try to create one more node
                with self.assertRaises(BackupException):
                    with backup.spawn_primary('node2') as node2:    # noqa
                        pass

    def test_backup_and_replication(self):
        with get_new_node('node') as node:
            node.init(allow_streaming=True)
            node.start()
            node.psql('postgres', 'create table abc(a int, b int)')
            node.psql('postgres', 'insert into abc values (1, 2)')

            backup = node.backup()

            with backup.spawn_replica('replica') as replica:
                replica.start()
                res = replica.execute('postgres', 'select * from abc')
                self.assertListEqual(res, [(1, 2)])

                # Insert into master node
                node.psql('postgres', 'insert into abc values (3, 4)')

                # Wait until data syncronizes
                replica.catchup()

                # Check that this record was exported to replica
                res = replica.execute('postgres', 'select * from abc')
                self.assertListEqual(res, [(1, 2), (3, 4)])

    def test_replicate(self):
        with get_new_node('node') as node:
            node.init(allow_streaming=True).start()

            with node.replicate(name='replica') as replica:
                res = replica.start().execute('postgres', 'select 1')
                self.assertListEqual(res, [(1, )])

                node.execute(
                    'postgres', 'create table test (val int)', commit=True)

                replica.catchup()

                res = node.execute('postgres', 'select * from test')
                self.assertListEqual(res, [])

    def test_incorrect_catchup(self):
        with get_new_node('node') as node:
            node.init(allow_streaming=True).start()

            # node has no master, can't catch up
            with self.assertRaises(CatchUpException):
                node.catchup()

    def test_dump(self):
        with get_new_node('node1') as node1:
            node1.init().start()

            with node1.connect('postgres') as con:
                con.begin()
                con.execute('create table test (val int)')
                con.execute('insert into test values (1), (2)')
                con.commit()

            # take a new dump
            dump = node1.dump('postgres')
            self.assertTrue(os.path.isfile(dump))

            with get_new_node('node2') as node2:
                node2.init().start().restore('postgres', dump)

                res = node2.execute('postgres',
                                    'select * from test order by val asc')
                self.assertListEqual(res, [(1, ), (2, )])

            # finally, remove dump
            os.remove(dump)

    def test_users(self):
        with get_new_node('master') as node:
            node.init().start()
            node.psql('postgres', 'create role test_user login')
            value = node.safe_psql('postgres', 'select 1', username='test_user')
            self.assertEqual(value, b'1\n')

    def test_poll_query_until(self):
        with get_new_node('master') as node:
            node.init().start()

            get_time = 'select extract(epoch from now())'
            check_time = 'select extract(epoch from now()) - {} >= 5'

            start_time = node.execute('postgres', get_time)[0][0]
            node.poll_query_until(
                dbname='postgres', query=check_time.format(start_time))
            end_time = node.execute('postgres', get_time)[0][0]

            self.assertTrue(end_time - start_time >= 5)

            # check 0 rows
            with self.assertRaises(QueryException):
                node.poll_query_until(
                    dbname='postgres',
                    query='select * from pg_class where true = false')

            # check 0 columns
            with self.assertRaises(QueryException):
                node.poll_query_until(
                    dbname='postgres', query='select from pg_class limit 1')

            # check None, fail
            with self.assertRaises(QueryException):
                node.poll_query_until(
                    dbname='postgres', query='create table abc (val int)')

            # check None, ok
            node.poll_query_until(
                dbname='postgres', query='create table def()',
                expected=None)    # returns nothing

            # check arbitrary expected value, fail
            with self.assertRaises(TimeoutException):
                node.poll_query_until(
                    dbname='postgres',
                    query='select 3',
                    expected=1,
                    max_attempts=3,
                    sleep_time=0.01)

            # check arbitrary expected value, ok
            node.poll_query_until(
                dbname='postgres', query='select 2', expected=2)

            # check timeout
            with self.assertRaises(TimeoutException):
                node.poll_query_until(
                    dbname='postgres',
                    query='select 1 > 2',
                    max_attempts=3,
                    sleep_time=0.01)

            # check ProgrammingError, fail
            with self.assertRaises(testgres.ProgrammingError):
                node.poll_query_until(dbname='postgres', query='dummy1')

            # check ProgrammingError, ok
            with self.assertRaises(TimeoutException):
                node.poll_query_until(
                    dbname='postgres',
                    query='dummy2',
                    max_attempts=3,
                    sleep_time=0.01,
                    raise_programming_error=False)

    def test_logging(self):
        logfile = tempfile.NamedTemporaryFile('w', delete=True)

        log_conf = {
            'version': 1,
            'handlers': {
                'file': {
                    'class': 'logging.FileHandler',
                    'filename': logfile.name,
                    'formatter': 'base_format',
                    'level': logging.DEBUG,
                },
            },
            'formatters': {
                'base_format': {
                    'format': '%(node)-5s: %(message)s',
                },
            },
            'root': {
                'handlers': ('file', ),
                'level': 'DEBUG',
            },
        }

        logging.config.dictConfig(log_conf)

        node_name = 'master'
        with get_new_node(node_name, use_logging=True) as master:
            master.init().start()

            # execute a dummy query a few times
            for i in range(20):
                master.execute('postgres', 'select 1')
                time.sleep(0.01)

            # let logging worker do the job
            time.sleep(0.1)

            # check that master's port is found
            with open(logfile.name, 'r') as log:
                lines = log.readlines()
                self.assertTrue(any(node_name in s for s in lines))

            # test logger after stop/start/restart
            master.stop()
            master.start()
            master.restart()
            self.assertTrue(master._logger.is_alive())

    @unittest.skipUnless(
        util_is_executable("pgbench"), "pgbench may be missing")
    def test_pgbench(self):
        with get_new_node('node') as node:
            node.init().start()

            # initialize pgbench
            node.pgbench_run(options=['-i'])

            # run TPC-B benchmark
            proc = node.pgbench(
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                options=['-T5'])

            out, _ = proc.communicate()
            out = out.decode('utf-8')

            self.assertTrue('tps' in out)

    def test_reload(self):
        with get_new_node('node') as node:
            node.init().start()

            cmd = "show client_min_messages"

            # change client_min_messages and save old value
            cmm_old = node.execute(dbname='postgres', query=cmd)
            node.append_conf('postgresql.conf', 'client_min_messages = DEBUG1')

            # reload config
            node.reload()

            # check new value
            cmm_new = node.execute(dbname='postgres', query=cmd)
            self.assertEqual('debug1', cmm_new[0][0].lower())
            self.assertNotEqual(cmm_old, cmm_new)

    def test_pg_ctl(self):
        with get_new_node('node') as node:
            node.init().start()

            status = node.pg_ctl(['status'])
            self.assertTrue('PID' in status)

    def test_ports_management(self):
        # check that no ports have been bound yet
        self.assertEqual(len(bound_ports), 0)

        with get_new_node('node') as node:
            # check that we've just bound a port
            self.assertEqual(len(bound_ports), 1)

            # check that bound_ports contains our port
            port_1 = list(bound_ports)[0]
            port_2 = node.port
            self.assertEqual(port_1, port_2)

        # check that port has been freed successfully
        self.assertEqual(len(bound_ports), 0)

    def test_version_management(self):
        a = LooseVersion('10.0')
        b = LooseVersion('10')
        c = LooseVersion('9.6.5')

        self.assertTrue(a > b)
        self.assertTrue(b > c)
        self.assertTrue(a > c)

    def test_config(self):
        # set global if it wasn't set
        configure_testgres(cache_initdb=True, cache_pg_config=True)

        # check same instances
        a = get_pg_config()
        b = get_pg_config()
        self.assertEqual(id(a), id(b))

        # modify setting
        configure_testgres(cache_pg_config=False)
        self.assertFalse(TestgresConfig.cache_pg_config)

        # check different instances
        a = get_pg_config()
        b = get_pg_config()
        self.assertNotEqual(id(a), id(b))

        # restore setting
        configure_testgres(cache_pg_config=True)

    def test_unix_sockets(self):
        with get_new_node('node') as node:
            node.init(unix_sockets=False, allow_streaming=True)
            node.start()

            node.execute('postgres', 'select 1')
            node.safe_psql('postgres', 'select 1')

            with node.replicate('r').start() as r:
                r.execute('postgres', 'select 1')
                r.safe_psql('postgres', 'select 1')

    def test_auto_name(self):
        with get_new_node().init(allow_streaming=True).start() as m:
            with m.replicate().start() as r:

                # check that nodes are running
                self.assertTrue(m.status())
                self.assertTrue(r.status())

                # check their names
                self.assertIsNotNone(m.name)
                self.assertIsNotNone(r.name)
                self.assertNotEqual(m.name, r.name)
                self.assertTrue('testgres' in m.name)
                self.assertTrue('testgres' in r.name)

    def test_file_tail(self):
        from testgres.utils import file_tail

        s1 = "the quick brown fox jumped over that lazy dog\n"
        s2 = "abc\n"
        s3 = "def\n"

        with tempfile.NamedTemporaryFile(mode='r+', delete=True) as f:
            for i in range(1, 5000):
                f.write(s1)
            f.write(s2)
            f.write(s3)

            f.seek(0)
            lines = file_tail(f, 3)
            self.assertEqual(lines[0], s1)
            self.assertEqual(lines[1], s2)
            self.assertEqual(lines[2], s3)

            f.seek(0)
            lines = file_tail(f, 1)
            self.assertEqual(lines[0], s3)

    def test_isolation_levels(self):
        with get_new_node('node').init().start() as node:
            with node.connect() as con:
                # string levels
                con.begin('Read Uncommitted').commit()
                con.begin('Read Committed').commit()
                con.begin('Repeatable Read').commit()
                con.begin('Serializable').commit()

                # enum levels
                con.begin(IsolationLevel.ReadUncommitted).commit()
                con.begin(IsolationLevel.ReadCommitted).commit()
                con.begin(IsolationLevel.RepeatableRead).commit()
                con.begin(IsolationLevel.Serializable).commit()

                # check wrong level
                with self.assertRaises(QueryException):
                    con.begin('Garbage').commit()


if __name__ == '__main__':
    unittest.main()
