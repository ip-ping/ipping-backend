"""IP data: the MaxMind GeoLite2 databases an administrator turns on, kept up to date, and what is built on them.

* `options`: what the administrator can set (and the write-only MaxMind credentials)
* `maxmind`: downloading and checking a database
* `updater`: keeping them up to date in the background
* `lookup`, `router`: the `/api/v1/ip` lookup API
* `admin`: the admin endpoints
"""
