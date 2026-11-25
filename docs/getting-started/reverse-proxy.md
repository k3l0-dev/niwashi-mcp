# Running behind a reverse proxy

Read this page if clients reach niwashi-mcp through a public name such as
`https://mcp.example.com/mcp`, with a proxy in front doing the TLS. If clients
use `http://localhost:8000/mcp` or a LAN address directly, you do not need it;
see [Quickstart](quickstart.md).

The server checks the `Host` header on requests that arrive on a loopback
address, to guard against DNS rebinding. A proxy on the same machine connects
over loopback and forwards its public name, so that name has to be allowed.

---

## 1 — Allow the public name

Add the hostname the proxy serves to `.env`:

```dotenv
MCP_ALLOWED_HOSTS=mcp.example.com
```

The value is a comma-separated list, and patterns work (`*.example.com`). It is
read when the server starts, so restart the server after changing it.

Nothing else changes. Keep `MCP_API_KEYS` set as you would without a proxy:
clients still send the key as `Authorization: Bearer` or `X-API-Key`, and
`/health` still needs no key.

---

## 2 — Configure the proxy

Both examples forward to the server on loopback. TLS settings are left out.

### Caddy

Caddy passes the `Host` header through unchanged, so this is the whole site
block:

```caddyfile
mcp.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

### nginx

nginx forwards the public `Host` only with `proxy_set_header Host $host;`.
The other settings below are recommended, not verified against niwashi-mcp.
They stop nginx buffering streamed responses and keep the connection open long
enough for slow tool calls:

```nginx
server {
    server_name mcp.example.com;
    # listen 443 ssl; ssl_certificate ...; (TLS lines elided)

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_http_version 1.1;
        proxy_buffering off;
        proxy_read_timeout 300s;
    }
}
```

---

## 3 — Verify

From a machine that can reach the public name:

```bash
curl -i https://mcp.example.com/health
```

You should get `200 OK` and the body `{"status": "ok"}`.
If you get `421`, the name in `MCP_ALLOWED_HOSTS` does not match the `Host` the
proxy sends. Restart the server after editing `.env`, then try again.

---

## Troubleshooting

- **After an upgrade, every request through the proxy, `/health` included,
  answers `421 Misdirected Request`.** The server is now checking `Host` and the
  public name is not allowed. Set `MCP_ALLOWED_HOSTS` to that name and restart.

---

## Who is unaffected

- **A proxy on another machine.** It reaches the server over a routable address,
  which is not `Host`-checked unless `MCP_ALLOWED_HOSTS` is set.
- **A LAN deployment with `MCP_HOST=0.0.0.0` and `MCP_API_KEYS`, and no proxy.**
  Its clients reach the server by names the server cannot know, and that
  continues to work unchanged.
- **Local clients on `http://127.0.0.1:8000/mcp`.** Their `Host` is `127.0.0.1`,
  which is always allowed.

The full rules are in the [settings reference](../configuration/settings.md#the-host-guard).
