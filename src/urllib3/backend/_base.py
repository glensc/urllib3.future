from __future__ import annotations

import enum
import socket
import typing
from base64 import b64encode
from datetime import datetime, timedelta
from secrets import token_bytes

if typing.TYPE_CHECKING:
    from ssl import SSLSocket, SSLContext, TLSVersion
    from .._typing import _TYPE_SOCKET_OPTIONS

from .._collections import HTTPHeaderDict
from .._constant import DEFAULT_BLOCKSIZE


class HttpVersion(str, enum.Enum):
    """Describe possible SVN protocols that can be supported."""

    h11 = "HTTP/1.1"
    # we know that it is rather "HTTP/2" than "HTTP/2.0"
    # it is this way to remain somewhat compatible with http.client
    # http_svn (int). 9 -> 11 -> 20 -> 30
    h2 = "HTTP/2.0"
    h3 = "HTTP/3.0"


class ConnectionInfo:
    def __init__(self) -> None:
        #: Time taken to establish the connection
        self.established_latency: timedelta | None = None

        #: HTTP protocol used with the remote peer (not the proxy)
        self.http_version: HttpVersion | None = None

        #: The SSL certificate presented by the remote peer (not the proxy)
        self.certificate_der: bytes | None = None
        self.certificate_dict: dict[
            str, int | tuple[tuple[str, str], ...] | tuple[str, ...] | str
        ] | None = None

        #: The SSL issuer certificate for the remote peer certificate (not the proxy)
        self.issuer_certificate_der: bytes | None = None
        self.issuer_certificate_dict: dict[
            str, int | tuple[tuple[str, str], ...] | tuple[str, ...] | str
        ] | None = None

        #: The IP address used to reach the remote peer (not the proxy), that was yield by your resolver.
        self.destination_address: tuple[str, int] | None = None

        #: The TLS cipher used to secure the exchanges (not the proxy)
        self.cipher: str | None = None
        #: The TLS revision used (not the proxy)
        self.tls_version: TLSVersion | None = None
        #: The time taken to reach a complete TLS liaison between the remote peer and us.  (not the proxy)
        self.tls_handshake_latency: timedelta | None = None
        #: Time taken to resolve a domain name into a reachable IP address.
        self.resolution_latency: timedelta | None = None

        #: Time taken to encode and send the whole request through the socket.
        self.request_sent_latency: timedelta | None = None

    def __repr__(self) -> str:
        return str(
            {
                "established_latency": self.established_latency,
                "certificate_der": self.certificate_der,
                "certificate_dict": self.certificate_dict,
                "issuer_certificate_der": self.issuer_certificate_der,
                "issuer_certificate_dict": self.issuer_certificate_dict,
                "destination_address": self.destination_address,
                "cipher": self.cipher,
                "tls_version": self.tls_version,
                "tls_handshake_latency": self.tls_handshake_latency,
                "http_version": self.http_version,
                "resolution_latency": self.resolution_latency,
                "request_sent_latency": self.request_sent_latency,
            }
        )

    def is_encrypted(self) -> bool:
        return self.certificate_der is not None


class LowLevelResponse:
    """Implemented for backward compatibility purposes. It is there to impose http.client like
    basic response object. So that we don't have to change urllib3 tested behaviors."""

    def __init__(
        self,
        method: str,
        status: int,
        version: int,
        reason: str,
        headers: HTTPHeaderDict,
        body: typing.Callable[[int | None, int | None], tuple[bytes, bool]] | None,
        *,
        authority: str | None = None,
        port: int | None = None,
        stream_id: int | None = None,
    ):
        self.status = status
        self.version = version
        self.reason = reason
        self.msg = headers
        self._method = method

        self.__internal_read_st = body
        self.closed = True if self.__internal_read_st is None else False
        self._eot = True if self.__internal_read_st is None else False

        # is kept to determine if we can upgrade conn
        self.authority = authority
        self.port = port

        self._stream_id = stream_id

        self.__buffer_excess: bytes = b""
        self.__promise: ResponsePromise | None = None

    @property
    def from_promise(self) -> ResponsePromise | None:
        return self.__promise

    @from_promise.setter
    def from_promise(self, value: ResponsePromise) -> None:
        if value.stream_id != self._stream_id:
            raise ValueError(
                "Trying to assign a ResponsePromise to an unrelated LowLevelResponse"
            )
        self.__promise = value

    @property
    def method(self) -> str:
        """Original HTTP verb used in the request."""
        return self._method

    def isclosed(self) -> bool:
        """Here we do not create a fp sock like http.client Response."""
        return self.closed

    def read(self, __size: int | None = None) -> bytes:
        if self.closed is True or self.__internal_read_st is None:
            # overly protective, just in case.
            raise ValueError(
                "I/O operation on closed file."
            )  # Defensive: Should not be reachable in normal condition

        if __size == 0:
            return b""  # Defensive: This is unreachable, this case is already covered higher in the stack.

        if self._eot is False:
            data, self._eot = self.__internal_read_st(__size, self._stream_id)

            # that's awkward, but rather no choice. the state machine
            # consume and render event regardless of your amt !
            if self.__buffer_excess:
                data = (  # Defensive: Difficult to put in place a scenario that verify this
                    self.__buffer_excess + data
                )
                self.__buffer_excess = b""  # Defensive:
        else:
            if __size is None:
                data = self.__buffer_excess
            else:
                data = self.__buffer_excess[:__size]
                self.__buffer_excess = self.__buffer_excess[__size:]

        if __size is not None and len(data) > __size:
            self.__buffer_excess = data[__size:]
            data = data[:__size]

        if self._eot and len(self.__buffer_excess) == 0:
            self.closed = True

        return data

    def close(self) -> None:
        self.__internal_read_st = None
        self.closed = True


class ResponsePromise:
    def __init__(
        self,
        conn: BaseBackend,
        stream_id: int,
        request_headers: list[tuple[bytes, bytes]],
        **parameters: typing.Any,
    ) -> None:
        self._uid: str = b64encode(token_bytes(16)).decode("ascii")
        self._conn: BaseBackend = conn
        self._stream_id: int = stream_id
        self._response: LowLevelResponse | None = None
        self._request_headers = request_headers
        self._parameters: typing.MutableMapping[str, typing.Any] = parameters

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ResponsePromise):
            return False
        return self.uid == other.uid

    def __repr__(self) -> str:
        return f"<ResponsePromise '{self.uid}' {self._conn._http_vsn_str} Stream[{self.stream_id}]>"

    @property
    def uid(self) -> str:
        return self._uid

    @property
    def request_headers(self) -> list[tuple[bytes, bytes]]:
        return self._request_headers

    @property
    def stream_id(self) -> int:
        return self._stream_id

    @property
    def is_ready(self) -> bool:
        return self._response is not None

    @property
    def response(self) -> LowLevelResponse:
        if not self._response:
            raise OSError
        return self._response

    @response.setter
    def response(self, value: LowLevelResponse) -> None:
        self._response = value

    def set_parameter(self, key: str, value: typing.Any) -> None:
        self._parameters[key] = value

    def get_parameter(self, key: str) -> typing.Any | None:
        return self._parameters[key] if key in self._parameters else None


_HostPortType: typing.TypeAlias = typing.Tuple[str, int]
QuicPreemptiveCacheType: typing.TypeAlias = typing.MutableMapping[
    _HostPortType, typing.Optional[_HostPortType]
]


class BaseBackend:
    """
    The goal here is to detach ourselves from the http.client package.
    At first, we'll strictly follow the methods in http.client.HTTPConnection. So that
    we would be able to implement other backend without disrupting the actual code base.
    Extend that base class in order to ship another backend with urllib3.
    """

    supported_svn: typing.ClassVar[list[HttpVersion] | None] = None
    scheme: typing.ClassVar[str]

    default_socket_kind: socket.SocketKind = socket.SOCK_STREAM
    #: Disable Nagle's algorithm by default.
    default_socket_options: typing.ClassVar[_TYPE_SOCKET_OPTIONS] = [
        (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1, "tcp")
    ]

    #: Whether this connection verifies the host's certificate.
    is_verified: bool = False

    #: Whether this proxy connection verified the proxy host's certificate.
    # If no proxy is currently connected to the value will be ``None``.
    proxy_is_verified: bool | None = None

    response_class = LowLevelResponse

    def __init__(
        self,
        host: str,
        port: int | None = None,
        timeout: int | float | None = -1,
        source_address: tuple[str, int] | None = None,
        blocksize: int = DEFAULT_BLOCKSIZE,
        *,
        socket_options: _TYPE_SOCKET_OPTIONS | None = default_socket_options,
        disabled_svn: set[HttpVersion] | None = None,
        preemptive_quic_cache: QuicPreemptiveCacheType | None = None,
    ):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.source_address = source_address
        self.blocksize = blocksize
        self.socket_kind = BaseBackend.default_socket_kind
        self.socket_options = socket_options
        self.sock: socket.socket | SSLSocket | None = None

        self._response: LowLevelResponse | None = None
        # Set it as default
        self._svn: HttpVersion | None = HttpVersion.h11

        self._tunnel_host: str | None = None
        self._tunnel_port: int | None = None
        self._tunnel_scheme: str | None = None
        self._tunnel_headers: typing.Mapping[str, str] = dict()

        self._disabled_svn = disabled_svn if disabled_svn is not None else set()
        self._preemptive_quic_cache = preemptive_quic_cache

        if self._disabled_svn:
            if HttpVersion.h11 in self._disabled_svn:
                raise RuntimeError(
                    "HTTP/1.1 cannot be disabled. It will be allowed in a future major version."
                )

        # valuable intel
        self.conn_info: ConnectionInfo | None = None

        self._promises: dict[str, ResponsePromise] = {}
        self._pending_responses: dict[int, LowLevelResponse] = {}

        self._start_last_request: datetime | None = None

    def __contains__(self, item: ResponsePromise) -> bool:
        return item.uid in self._promises

    @property
    def disabled_svn(self) -> set[HttpVersion]:
        return self._disabled_svn

    @property
    def _http_vsn_str(self) -> str:
        """Reimplemented for backward compatibility purposes."""
        assert self._svn is not None
        return self._svn.value

    @property
    def _http_vsn(self) -> int:
        """Reimplemented for backward compatibility purposes."""
        assert self._svn is not None
        return int(self._svn.value.split("/")[-1].replace(".", ""))

    @property
    def is_saturated(self) -> bool:
        raise NotImplementedError

    @property
    def is_idle(self) -> bool:
        return not self._promises and not self._pending_responses

    @property
    def is_multiplexed(self) -> bool:
        raise NotImplementedError

    def _upgrade(self) -> None:
        """Upgrade conn from svn ver to max supported."""
        raise NotImplementedError

    def _tunnel(self) -> None:
        """Emit proper CONNECT request to the http (server) intermediary."""
        raise NotImplementedError

    def _new_conn(self) -> socket.socket | None:
        """Run protocol initialization from there. Return None to ensure that the child
        class correctly create the socket / connection."""
        raise NotImplementedError

    def _post_conn(self) -> None:
        """Should be called after _new_conn proceed as expected.
        Expect protocol handshake to be done here."""
        raise NotImplementedError

    def _custom_tls(
        self,
        ssl_context: SSLContext | None = None,
        ca_certs: str | None = None,
        ca_cert_dir: str | None = None,
        ca_cert_data: None | str | bytes = None,
        ssl_minimum_version: int | None = None,
        ssl_maximum_version: int | None = None,
        cert_file: str | None = None,
        key_file: str | None = None,
        key_password: str | None = None,
    ) -> None:
        """This method serve as bypassing any default tls setup.
        It is most useful when the encryption does not lie on the TCP layer. This method
        WILL raise NotImplementedError if the connection is not concerned."""
        raise NotImplementedError

    def set_tunnel(
        self,
        host: str,
        port: int | None = None,
        headers: typing.Mapping[str, str] | None = None,
        scheme: str = "http",
    ) -> None:
        """Prepare the connection to set up a tunnel. Does NOT actually do the socket and http connect.
        Here host:port represent the target (final) server and not the intermediary."""
        raise NotImplementedError

    def putrequest(
        self,
        method: str,
        url: str,
        skip_host: bool = False,
        skip_accept_encoding: bool = False,
    ) -> None:
        """It is the first method called, setting up the request initial context."""
        raise NotImplementedError

    def putheader(self, header: str, *values: str) -> None:
        """For a single header name, assign one or multiple value. This method is called right after putrequest()
        for each entries."""
        raise NotImplementedError

    def endheaders(
        self,
        message_body: bytes | None = None,
        *,
        encode_chunked: bool = False,
        expect_body_afterward: bool = False,
    ) -> ResponsePromise | None:
        """This method conclude the request context construction."""
        raise NotImplementedError

    def getresponse(
        self, *, promise: ResponsePromise | None = None
    ) -> LowLevelResponse:
        """Fetch the HTTP response. You SHOULD not retrieve the body in that method, it SHOULD be done
        in the LowLevelResponse, so it enable stream capabilities and remain efficient.
        """
        raise NotImplementedError

    def close(self) -> None:
        """End the connection, do some reinit, closing of fd, etc..."""
        raise NotImplementedError

    def send(
        self,
        data: (bytes | typing.IO[typing.Any] | typing.Iterable[bytes] | str),
        *,
        eot: bool = False,
    ) -> ResponsePromise | None:
        """The send() method SHOULD be invoked after calling endheaders() if and only if the request
        context specify explicitly that a body is going to be sent."""
        raise NotImplementedError
