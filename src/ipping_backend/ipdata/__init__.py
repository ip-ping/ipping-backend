"""IP data: nine sources, each with a switch, asked together and voted on, and what is built on them.

* `catalog`: the nine sources and what each needs; `options`: the switches and the write-only keys
* `local`: the five that live on disk (MaxMind and DB-IP as `.mmdb`, IPtoASN and IPdeny as range tables, ip2region as `.xdb`);
  `ranges`, `xdb`, `maxmind`: reading and downloading them
* `online`: the four services asked over HTTPS (IPinfo, IPLocate, ipapi.is, IP2Location)
* `records`, `consensus`: one shape for what a source says, and the majority vote with its percentage
* `service`: asking every source that is on; `downloader`, `updater`: keeping the databases up to date in the background
* `lookup`, `router`, `admin`: the `/api/v1/ip` lookup API and the admin endpoints
"""
