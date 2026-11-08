# How eticket.uzrailpass.uz works (and why the bot does what it does)

These notes come from reading the website's own requests and measuring them in November 2026. Everything here
is what the public website sends from a browser. If the railway changes its site, details below can go out of
date. The bot then reports the raw error in Telegram.

## Session

- `GET /api/v1/csrf-token` sets an `XSRF-TOKEN` cookie; every later call echoes it as the `X-XSRF-TOKEN` header,
  plus `device-type: BROWSER` and `Accept-Language: en|ru|uz`.
- `POST /api/v1/auth/login {username, password}` returns a JWT (60 minutes, sent as `Authorization: Bearer`).
  The bot logs in again shortly before it expires.
- HTTP/1.1 only. The server closes idle keep-alive connections after about 1 second:

  | Request | Time |
  | --- | --- |
  | On a warm connection | 40 to 120 ms |
  | On a new connection | 440 to 570 ms |

  That is why the bot checks every 0.8 s, while its connection is still open.

## Endpoints the bot uses

| What | Request |
| --- | --- |
| Trains on a date (with free seats per class) | `POST /api/v3/handbook/trains/list` `{directions:{forward:{date, depStationCode, arvStationCode}}, routeType:"INTERCITY"}` |
| Cars and free seats of one train | `POST /api/v1/handbook/trains` `{depDate, depStationCode, arvStationCode, trainNumber, trainId:null}` (HTTP 204 when sold out) |
| Saved passengers | `POST /api/v1/users/friend/list {userId}` |
| Reserve | `POST /api/v3/universal-orders/reserve` |
| Payment deadline | `GET /api/v3/universal-orders/query/get/payment-end-time/{orderId}` |
| Cancel | `POST /api/v3/universal-orders/cancel` |
| Orders | `POST /api/v3/query/orders/list {page, length}` |
| Ticket PDF | `GET /api/v1/document/{orderId}/pdf` |
| Live order events | websocket `wss://eticket.uzrailpass.uz/ws`, send `{group: userId, subscribe: 1}` |

Sold-out trains are still listed, so the train numbers are known before any seat appears. There is no live
seat feed: availability has to be polled. The websocket only carries events for your own orders.

Class codes are the same in every language; the class names change with the language.

| Code | Class |
| --- | --- |
| `3П` | Plaskart (open sleeper) |
| `2К` | Kupe (4-berth) |
| `1Л` | SV / lux (2-berth) |
| `2Е` | Sitting, economy |
| `1С` | Sitting, business |
| `2В` | Sitting, 2nd class |
| `1В` | Sitting, 1st class |

## How booking works

The reserve body is the website's own. Two fields are required:

- `carNumber`: the request fails without it.
- `reserveSeatRequirements.seatsRange`: `"1-99"` lets the railway pick the seats.

Up to 4 passengers fit in one order.

| Time after reserve | What happens |
| --- | --- |
| ~80 ms | The reply is an order id. Nothing is held yet. |
| ~0.12 s | websocket `ORDER_CREATED` |
| ~1.1 to 2 s | websocket `PAYMENT_STARTED`: the seats are held and the full order is included. Or `ORDER_FAILED`: the seat was already taken. |
| 12 min | Payment deadline. After it, the railway releases the seats. |

- The order snapshot endpoint (`get-snapshot`) is heavily rate-limited, so the bot waits on the websocket
  instead.
- The same passenger can hold two orders at once, so the bot itself has to make sure it books each passenger
  only once.

## Timing measured on the live site

- A freed seat is invisible to everyone for a while. When a hold is cancelled, the train list showed the seat
  free 1.8 s later in one test and 8.7 s later in another. This delay is on the railway's side.
- The car lookup is cached for 3 to 5 s per train and date. Right after the train list shows a seat, the car
  lookup can still say there is none, and a bot that trusts it misses the seat. So the bot remembers each
  train's car numbers per class and date and reserves on them directly at the moment of detection, with the
  car lookup running in parallel as a fallback.
- Car numbers change between dates and routes, so the learned cars are kept per date first.
- The rate limit is per endpoint, and a 429 comes without a `Retry-After` header. On 6 Nov it appeared at about
  47 requests per second on `get-snapshot`. On 8 Nov the reserve endpoint answered 429 after a bot retried one
  seat that could not be booked about 4 times a second for minutes; that lock lasted more than 6 minutes. The
  bot now keeps all requests under 10 per second, leaves a seat that just failed alone until the train list
  changes, and pauses an endpoint after a 429, longer each time it repeats (30 s, then up to 300 s).
- Real catches on a sold-out train (7 Nov 2026):

  | Catch | Visible on the site → held |
  | --- | --- |
  | Seat that appeared | 3.3 s. The train's car of the day was not learned yet. |
  | Seat someone had just cancelled | 2.8 s |

  Since then, the bot keys the car cache by date and sends the first reserve on the warm connection.

Reserving every seat blindly every second was rejected. Each reserve creates a real order in the railway's
booking system, so that would mean thousands of failed orders per hour per user and an account ban, and it
still could not beat polling the train list, which is one cheap read that creates nothing.

## Payment without a browser (Hamkorbank)

1. `POST /api/v1/payment/select-payment-type`
   `{id: orderId, paymentId: orderId + "_PAYMENT", type: "HamkorbankWebView", withLoyaltyProgram: null}`.
   The websocket answers `SELECT_PAYMENT_SUCCEEDED` with a `paymentSystemId`.
2. `POST /api/v1/hamkorbank-web-view/prepare-payment {id: paymentSystemId, successUrl, failedUrl}`.
   The websocket answers `PREPARE_PAYMENT_SUCCEEDED` with a `payUrl` on `acq-invoice.hamkorbank.uz`.
   Telegram `t.me/<bot>?start=…` links are accepted as return URLs.
3. You pay on the bank's page (card and SMS code). The websocket then reports `PAYMENT_SUCCESS` and
   `ORDER_COMPLETED_SUCCESSFULLY`, or `PAYMENT_FAIL`.
4. `POST /api/v1/hamkorbank-web-view/callback-status` is what the website calls when you come back from
   the bank. Calling it before paying makes the server drop the connection, so the bot calls it only on
   return.

From held seats to a ready Pay link takes about 0.5 s.

## Telegram mini app without a server

The mini app (`docs/`) is static.

- The bot puts the current settings in the URL fragment of the keyboard button that opens the mini app.
  Telegram updates that link only when a bot message carries the keyboard, so every plain bot message carries
  a fresh one; otherwise a change made in the PC app would not show in the mini app.
- The mini app sends changes back with `Telegram.WebApp.sendData` (at most 4096 bytes), which works only from
  a reply-keyboard button.
- The railway site sends no CORS headers, so the mini app ships its own station list in `stations.json`,
  built with `tools/build_stations.py`.
- The mini app encrypts the railway password with the PC's RSA public key (RSA-OAEP, SHA-256), so it never
  crosses Telegram in plain text.
