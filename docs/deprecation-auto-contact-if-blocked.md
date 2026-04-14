# Deprecation: `auto_contact_if_blocked`

**Status:** Deprecated as of `fix/step-2-shadow-create-root-cause` (2026-04).
**Removal target:** one release after deprecation.

## Why

Silent handshake-on-block erodes the contact-discipline the server is meant
to enforce. Callers should establish contact links explicitly via
`macro_contact_handshake` so that approval state is intentional and
auditable, not a side-effect of a send.

## Migration

Before:
```python
send_message(..., auto_contact_if_blocked=True)
```

After:
```python
macro_contact_handshake(
    project_key=...,
    requester=sender,
    target=recipient,
    to_project=<recipient project_key>,
    auto_accept=False,  # True only if you own both sides
)
# ...then:
send_message(..., to_project=<recipient project_key>)
```

## Timeline

- **Now:** `auto_contact_if_blocked=True` logs a `WARNING` but still works.
- **Next release:** parameter removed. Callers using it will get a
  `TypeError: send_message() got an unexpected keyword argument
  'auto_contact_if_blocked'`.

## Server-side setting also flipped

The server-side companion knob `MESSAGING_AUTO_HANDSHAKE_ON_BLOCK` used to
default to `true`, which meant even callers that left
`auto_contact_if_blocked=False` (the default) were getting the silent
handshake behavior. That broke the principle of least astonishment:
deprecating the caller-facing param while the server-side knob stayed
on-by-default was half a fix.

**New default (2026-04):** `MESSAGING_AUTO_HANDSHAKE_ON_BLOCK=false`.

If an operator explicitly sets it back to `true` (via environment variable
or `.env`), the server logs a loud `WARNING` at startup naming the setting
and pointing at this document. Setting it to `true` re-enables the same
silent-handshake anti-pattern described in **Why** above; prefer calling
`macro_contact_handshake` explicitly instead.
