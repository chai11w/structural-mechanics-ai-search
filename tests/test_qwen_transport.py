from __future__ import annotations

import errno
import http.client
import io
import socket
import ssl
import unittest
from unittest.mock import Mock, patch
import urllib.request

from tiku_shared.qwen_transport import (
    QWEN_CONNECT_RETRIES_ENV,
    QwenConnectRetryHTTPSConnection,
    QwenConnectRetryHTTPSHandler,
    _TCPConnectRetry,
    configure_qwen_connect_retries_from_env,
)


class FakeSocket:
    def __init__(self, *, send_error=None, read_error=None, body_read_error=None):
        self.sent = []
        self.closed = False
        self.send_error = send_error
        self.read_error = read_error
        self.body_read_error = body_read_error

    def setsockopt(self, *args):
        pass

    def sendall(self, data):
        self.sent.append(data)
        if self.send_error is not None:
            raise self.send_error

    def makefile(self, *args):
        if self.read_error is not None:
            raise self.read_error
        if self.body_read_error is not None:
            class FailingBody(io.BytesIO):
                def read(inner, *args):
                    raise self.body_read_error
            return FailingBody(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
        return io.BytesIO(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")

    def close(self):
        self.closed = True


def fake_context(*, handshake_error=None):
    context = Mock()
    if handshake_error is None:
        context.wrap_socket.side_effect = lambda value, **kwargs: value
    else:
        context.wrap_socket.side_effect = handshake_error
    return context


class QwenTransportTests(unittest.TestCase):
    def setUp(self):
        self.saved_opener = urllib.request._opener
        urllib.request._opener = None

    def tearDown(self):
        urllib.request._opener = self.saved_opener

    def connection(self, connector, *, context=None):
        with patch("socket.create_connection", connector):
            return QwenConnectRetryHTTPSConnection("dashscope.aliyuncs.com", timeout=10, context=context or fake_context())

    def test_failed_tcp_then_success_sends_one_http_request_and_keeps_source_address(self):
        wire = FakeSocket()
        connector = Mock(side_effect=[ConnectionRefusedError(errno.ECONNREFUSED, "synthetic"), wire])
        connection = self.connection(connector)
        connection.source_address = ("127.0.0.1", 0)
        connection.request("POST", "/test", body=b"{}")
        self.assertEqual(connection.getresponse().read(), b"{}")
        self.assertEqual(connector.call_count, 2)
        self.assertEqual(sum(data.startswith(b"POST ") for data in wire.sent), 1)
        self.assertEqual(connector.call_args.args[2], ("127.0.0.1", 0))
        self.assertEqual(connection.qwen_tcp_connector.connection_attempts, 2)

    def test_tls_failure_does_not_retry_connection_or_send_http(self):
        wire = FakeSocket()
        connector = Mock(return_value=wire)
        connection = self.connection(connector, context=fake_context(handshake_error=ssl.SSLError("synthetic")))
        with self.assertRaises(ssl.SSLError):
            connection.request("POST", "/test", body=b"{}")
        self.assertEqual(connector.call_count, 1)
        self.assertEqual(wire.sent, [])
        connection.close()

    def test_http_send_failure_is_not_retried(self):
        wire = FakeSocket(send_error=ConnectionResetError(errno.ECONNRESET, "synthetic"))
        connector = Mock(return_value=wire)
        connection = self.connection(connector)
        with self.assertRaises(ConnectionResetError):
            connection.request("POST", "/test", body=b"{}")
        self.assertEqual(connector.call_count, 1)
        self.assertEqual(sum(data.startswith(b"POST ") for data in wire.sent), 1)
        connection.close()

    def test_read_failure_is_not_retried(self):
        wire = FakeSocket(read_error=http.client.RemoteDisconnected("synthetic"))
        connector = Mock(return_value=wire)
        connection = self.connection(connector)
        connection.request("POST", "/test", body=b"{}")
        with self.assertRaises(http.client.RemoteDisconnected):
            connection.getresponse()
        self.assertEqual(connector.call_count, 1)
        self.assertEqual(sum(data.startswith(b"POST ") for data in wire.sent), 1)
        connection.close()

    def test_response_body_read_timeout_is_not_retried(self):
        wire = FakeSocket(body_read_error=TimeoutError("synthetic"))
        connector = Mock(return_value=wire)
        connection = self.connection(connector)
        connection.request("POST", "/test", body=b"{}")
        response = connection.getresponse()
        with self.assertRaises(TimeoutError):
            response.read()
        self.assertEqual(connector.call_count, 1)
        self.assertEqual(sum(data.startswith(b"POST ") for data in wire.sent), 1)
        response.close()
        connection.close()

    def test_only_temporary_dns_errors_retry(self):
        connector = Mock(side_effect=[socket.gaierror(socket.EAI_AGAIN, "synthetic"), FakeSocket()])
        _TCPConnectRetry(connector)(("example.test", 443), 10)
        self.assertEqual(connector.call_count, 2)
        connector = Mock(side_effect=socket.gaierror(socket.EAI_NONAME, "synthetic"))
        with self.assertRaises(socket.gaierror):
            _TCPConnectRetry(connector)(("example.test", 443), 10)
        self.assertEqual(connector.call_count, 1)

    def test_windows_connect_timeout_retries_but_arbitrary_oserror_does_not(self):
        windows_error = OSError("synthetic")
        windows_error.winerror = 10060
        connector = Mock(side_effect=[windows_error, FakeSocket()])
        _TCPConnectRetry(connector)(("example.test", 443), 10)
        self.assertEqual(connector.call_count, 2)
        connector = Mock(side_effect=PermissionError(errno.EACCES, "synthetic"))
        with self.assertRaises(PermissionError):
            _TCPConnectRetry(connector)(("example.test", 443), 10)
        self.assertEqual(connector.call_count, 1)

    def test_retry_uses_remaining_timeout_and_stops_when_budget_is_spent(self):
        connector = Mock(side_effect=[TimeoutError("synthetic"), FakeSocket()])
        with patch("tiku_shared.qwen_transport.time.monotonic", side_effect=[0, 0, 4, 4, 5]):
            _TCPConnectRetry(connector)(("example.test", 443), 10)
        self.assertEqual([call.args[1] for call in connector.call_args_list], [10, 6])
        connector = Mock(side_effect=TimeoutError("synthetic"))
        with patch("tiku_shared.qwen_transport.time.monotonic", side_effect=[0, 0, 10]):
            with self.assertRaises(TimeoutError):
                _TCPConnectRetry(connector)(("example.test", 443), 10)
        self.assertEqual(connector.call_count, 1)

    def test_overdue_success_is_closed_before_any_http_send(self):
        wire = FakeSocket()
        connector = Mock(return_value=wire)
        with patch("tiku_shared.qwen_transport.time.monotonic", side_effect=[0, 0, 11]):
            with self.assertRaises(TimeoutError):
                _TCPConnectRetry(connector)(("example.test", 443), 10)
        self.assertTrue(wire.closed)
        self.assertEqual(wire.sent, [])

    def test_connection_exhaustion_is_bounded_to_two_attempts(self):
        connector = Mock(side_effect=ConnectionRefusedError(errno.ECONNREFUSED, "synthetic"))
        with self.assertRaises(ConnectionRefusedError):
            _TCPConnectRetry(connector)(("example.test", 443), 10)
        self.assertEqual(connector.call_count, 2)

    def test_nonblocking_timeout_keeps_one_original_connection_attempt(self):
        connector = Mock(side_effect=ConnectionRefusedError(errno.ECONNREFUSED, "synthetic"))
        with self.assertRaises(ConnectionRefusedError):
            _TCPConnectRetry(connector)(("example.test", 443), 0)
        self.assertEqual(connector.call_count, 1)
        self.assertEqual(connector.call_args.args[1], 0)

    def test_non_target_hosts_use_original_https_connection_and_tls_context(self):
        context = ssl.create_default_context()
        handler = QwenConnectRetryHTTPSHandler(context=context)
        for host in ("open.bigmodel.cn", "sub.dashscope.aliyuncs.com", "dashscope.aliyuncs.com.evil.test"):
            with self.subTest(host=host), patch.object(handler, "do_open", return_value="untouched") as do_open:
                request = urllib.request.Request("https://" + host + "/test")
                self.assertEqual(handler.https_open(request), "untouched")
                self.assertIs(do_open.call_args.args[0], http.client.HTTPSConnection)
                self.assertIs(do_open.call_args.kwargs["context"], context)

    def test_target_host_uses_retry_connection_and_original_tls_context(self):
        context = ssl.create_default_context()
        handler = QwenConnectRetryHTTPSHandler(context=context)
        with patch.object(handler, "do_open") as do_open:
            request = urllib.request.Request("https://dashscope.aliyuncs.com/test")
            handler.https_open(request)
            connection = do_open.call_args.args[0]("dashscope.aliyuncs.com", context=context)
        self.assertIsInstance(connection, QwenConnectRetryHTTPSConnection)
        self.assertIs(connection._context, context)
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_default_and_zero_env_do_not_install_or_change_opener(self):
        original = urllib.request.build_opener()
        urllib.request.install_opener(original)
        with patch("urllib.request.install_opener") as install:
            self.assertFalse(configure_qwen_connect_retries_from_env({}))
            self.assertFalse(configure_qwen_connect_retries_from_env({QWEN_CONNECT_RETRIES_ENV: "0"}))
        install.assert_not_called()
        self.assertIs(urllib.request._opener, original)

    def test_enabled_config_preserves_existing_proxy_and_tls_without_mutating_old_opener(self):
        context = ssl.create_default_context()
        proxies = {"https": "http://127.0.0.1:7897", "no": "localhost"}
        original_proxy = urllib.request.ProxyHandler(proxies)
        original_https = urllib.request.HTTPSHandler(context=context)
        original = urllib.request.build_opener(original_proxy, original_https)
        urllib.request.install_opener(original)
        self.assertTrue(configure_qwen_connect_retries_from_env({QWEN_CONNECT_RETRIES_ENV: "1"}))
        configured = urllib.request._opener
        proxy = next(handler for handler in configured.handlers if isinstance(handler, urllib.request.ProxyHandler))
        https = next(handler for handler in configured.handlers if isinstance(handler, QwenConnectRetryHTTPSHandler))
        self.assertEqual(proxy.proxies, proxies)
        self.assertIs(https._context, context)
        self.assertIs(original_proxy.parent, original)
        self.assertIs(original_https.parent, original)

    def test_only_zero_and_one_are_accepted(self):
        with patch("urllib.request.install_opener") as install:
            for value in ("2", "-1", "true", "", " 1 "):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    configure_qwen_connect_retries_from_env({QWEN_CONNECT_RETRIES_ENV: value})
        install.assert_not_called()

    def test_unknown_custom_https_handler_is_not_silently_replaced(self):
        class CustomHTTPSHandler(urllib.request.HTTPSHandler):
            pass
        original = urllib.request.build_opener(CustomHTTPSHandler())
        urllib.request.install_opener(original)
        with self.assertRaises(ValueError):
            configure_qwen_connect_retries_from_env({QWEN_CONNECT_RETRIES_ENV: "1"})
        self.assertIs(urllib.request._opener, original)

    def test_tcp_retry_does_not_create_another_model_cost_attempt(self):
        from tiku_shared.model_costs import ModelCostCollector, model_cost_scope, timed_model_call

        wire = FakeSocket()
        connector = Mock(side_effect=[TimeoutError("synthetic"), wire])
        connection = self.connection(connector)

        def one_http_request():
            connection.request("POST", "/test", body=b"{}")
            return connection.getresponse().read()

        collector = ModelCostCollector(run_id="synthetic-qwen-connect-test")
        with model_cost_scope(collector):
            self.assertEqual(timed_model_call(one_http_request, provider="dashscope", model="qwen-test",
                call_type="qwen_connect_test", usage_getter=lambda _: {"total_tokens": 1}), b"{}")
        records = collector.records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].attempt_count, 1)
        self.assertEqual(connector.call_count, 2)


if __name__ == "__main__":
    unittest.main()
