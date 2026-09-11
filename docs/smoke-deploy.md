# Smoke gate before a deploy

`scripts/smoke_deploy.py` runs the exact image that is about to be deployed and walks the real
user journey against it over HTTP. It exists because three bugs reached production in one day,
all the same shape: the Python server and the JavaScript client used different names for the same
JSON field. Each one was found by a person using the app, never by the test suite, because both
sides only ever tested themselves.

The unit tests answer "is each side correct on its own". This answers a different question:
**does the image about to be deployed actually work when a person walks through it end to end?**

## Running it

```powershell
.venv\Scripts\python.exe scripts\smoke_deploy.py registry.ktek-group.pt/cafe/cafe-escritorio:0.7.0
```

An image reference is required and can be a tag or a digest. The image is pulled if it is not
present locally, or always with `--pull`. Exit code 0 means every step ran and gave the right
answer; anything else means do not deploy.

It needs Docker and the packages `requirements.txt` already installs: `pywebpush` brings
`http-ece`, `cryptography` and `requests`, which is everything the script imports beyond the
standard library. No extra dependency was added for it.

## What it checks

1. The container starts and `GET /` answers 200.
2. `GET /sw.js` and `GET /static/app.js` are byte identical to the files in the working tree.
   This catches an image built from a different tree than the one being tested, which is
   invisible to every other check. A mismatch is always a failure, but the message
   distinguishes two causes, because the fix differs: the content really differs, or only the
   line endings do (an image built from a Windows checkout that git had not renormalized, which
   `.gitattributes` asks to be LF). The bytes are never normalized before comparing: doing that
   to silence the second case would blind the check to the first.
3. Register, log out, log in again, `POST /api/cafe`, then `GET /api/eu` and assert the coffee
   count actually moved to 1. The assertion is on the effect, not on the 201.
4. `GET /api/push/chave` carries the field the client reads. The field name is extracted from
   `app/static/app.js` (`chaveVapid = dados.<field>`) at run time, never hardcoded here, and the
   value must be non-empty and equal to the key the container was started with.
5. `PUT /api/notificacoes/preferencias` with exactly the payload shape `app.js` builds (one key
   per `data-evento` toggle in `index.html`), then `GET` it back and assert it persisted.
6. **The push payload, decrypted.** A real P-256 subscription keypair is generated, a local HTTP
   server stands in for the push service, a second person drinks a coffee (the app never notifies
   the author of an event), and the received body is decrypted with `http_ece`. Every field the
   `push` handler in `app/static/sw.js` reads (`dados.<field>`, extracted from the listener) must
   be present **and non-empty**. Asserting on the keys alone would let the empty title bug
   through, which is the bug that shipped.

## It never touches production

The container is created by this script with an explicit environment, so no ambient `CAFE_*`
variable can leak in. The VAPID keypair is generated fresh per run, the database is a tmpfs that
dies with the container, and the push endpoint points at a local stand in server, never at a real
push service. The container is removed and the port freed on every exit path, including a failed
step and Ctrl-C.

## Skips are failures by default

If a check cannot be run honestly, the script prints a `SKIPPED` line naming exactly what went
unverified and why, and still exits non-zero. `--allow-skips` exits 0 instead, after printing the
same lines. A gate that quietly drops its hardest check turns "unverified" into a green tick,
which is the failure this script was written to prevent, so opting out of a check is a decision a
person makes on purpose and not a default.

## Proving the checks are load bearing

A check that cannot fail is decoration. Steps 2, 3, 4 and 6 were verified by breaking them on
purpose and confirming the script exits non-zero naming what broke: a coffee count that cannot
happen, a client field name that does not exist in the response, a field the payload does not
carry, a field present but empty, and both diagnoses of an asset mismatch. Worth repeating after
any change to the script.

## Known limits, so nobody mistakes them for tested ground

Verified by running: the happy path against a matching image, and the failure paths above.
Not exercised yet, and therefore not proven: the Ctrl-C and SIGTERM cleanup handlers (cleanup is
proven on the normal and failed-step paths only), the `--pull` branch and digest references, hosts
where `host.docker.internal` is not provided by Docker Desktop, and `--allow-skips`. That last one
is a no-op today: every step turned out to be implementable honestly, so nothing raises a skip and
the flag has never changed an outcome. It is scaffolding for a future check that cannot be done
honestly, not a tested escape hatch.
