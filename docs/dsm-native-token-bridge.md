# DSM 7 native token transport

DSM browser login uses its native `_SSID` cookie. When SynoToken checking is enabled,
that cookie alone does not satisfy `authenticate.cgi`; the request also needs DSM's
native token. An API-created `session=DSM` cookie does not reproduce this browser
flow. The gateway uses the documented same-origin endpoint:

`GET /webapi/entry.cgi?api=SYNO.API.Auth&version=6&method=token`

Reference: <https://kb.synology.com/zh-tw/DG/DSM_Login_Web_API_Guide/3>.

## Request flow

An index navigation which fails native authentication receives a small bootstrap
page. Its CSP allows only local scripts and same-origin fetches, disallows framing,
forms, base changes and other resources, and includes no request secrets. The fixed
`/_dsm/bootstrap.js` route serves only the bundled script and is public so that an
unvalidated browser can start the native login check. These are the only public
content routes; API, video, thumbnail, and app static requests remain protected.

The script obtains `data.synotoken` from DSM and posts JSON containing only `token`
to the fixed gateway route `/_dsm/token`. It does not accept an API URL or forward an
arbitrary browser request. The token fetch explicitly uses
`referrerPolicy: 'same-origin'`: DSM 7.4 rejects it with API error 400 if the page's default
`Referrer-Policy: no-referrer` suppresses its referrer. This override sends the
package document URL only to the fixed same-origin DSM endpoint; no token is in
that URL. The page's default remains `no-referrer` for other requests and links.
The bridge requires an exact Origin equal to the CGI
request's scheme and Host, `application/json`, and a body of at most 4096 bytes.
It derives the scheme from `HTTPS` or `REQUEST_SCHEME`, never forwarded headers or
the backend server port. QuickConnect HTTPS requests and direct DSM HTTP on port
5000 therefore use their actual public scheme and Host.

Before issuing a cookie, the bridge runs DSM's native checker using the candidate
`X-SYNO-TOKEN` and the current DSM cookies. Failure returns 401 and issues no token.
Success stores the native token in a `camera_dsm_token` cookie with these attributes:

- `HttpOnly`, `SameSite=Strict`, and `Secure` on HTTPS.
- Path restricted to the package's `index.cgi/` prefix, with no Domain.
- Browser-session lifetime: no Expires or Max-Age.

This is intentional transport of DSM's native token for `<video>` and `<img>`
requests, which cannot attach a custom header. It is not an authenticated identity
session. No identity or token is persisted on the NAS, and no extra session server
or login authority is introduced.

Every protected request invokes `authenticate.cgi` again with the current DSM
cookies. An explicit browser `X-SYNO-TOKEN` takes precedence; otherwise the gateway
supplies the transport cookie's token to the native checker. It removes that
transport cookie before invoking DSM and preserves the other DSM cookies. Revoked
or expired DSM cookies fail even if an old token transport cookie remains. Only the
existing signed gateway user assertion reaches the container: DSM cookies, native
tokens, and incoming identity headers are excluded. SynoToken query parameters are
rejected instead of being forwarded to the app. The bridge never places a token in
a URL, page, log, browser localStorage/sessionStorage, or API response body.

## Failure handling

Token retrieval errors, missing `data.synotoken`, and native validation failures
show a DSM sign-in link. Navigating to the index with an expired transport can fetch
a new native token and replace it. After a successful bridge, the page reloads once.
A nonsecret timestamp in sessionStorage stops another automatic bridge for 60
seconds if the reloaded index still cannot authenticate (for example, blocked
cookies or a concurrently revoked DSM session). It contains no token or identity.
During this cooldown the page shows the sign-in link; another index navigation
after the cooldown can retry. Browsers that block sessionStorage receive an explicit
open-page link after the bridge succeeds, avoiding an unbounded reload loop.

## Local verification

`tests/test_gateway.py` exercises the real gateway with a loopback HTTP upstream;
the DSM checker is the substituted system boundary. It covers bootstrap isolation,
strict origins and schemes, incorrect/revoked credentials, cookie attributes,
bounded and malformed JSON, explicit-header precedence, per-request native checks
for API/media/thumbnails/static, upstream token exclusion, and streaming.

`tests/test_dsm_bootstrap.js` runs the actual bundled script with browser/network
boundaries substituted. It verifies the official token endpoint, fixed JSON POST,
failure and missing-token handling, reload-loop prevention, expired-transport retry,
and disabled-storage fallback. `tests/test_spk.py` verifies deterministic packaging
of this script and package version `1.0.0-1006`.

Live DSM/QuickConnect verification is a separate deployment acceptance step; local
tests cannot establish native checker or browser cookie behavior on a particular NAS.
