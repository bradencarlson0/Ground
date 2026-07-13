#!/usr/bin/env python3
"""Bootstrap the reviewed supplemental opening-source research crawler."""
from urllib.request import Request, urlopen

URL = "https://raw.githubusercontent.com/bradencarlson0/Ground/run-yext-extractor/scripts/research_36_openings.py"
request = Request(URL, headers={"User-Agent": "BC-Land-USA-opening-research/1.0"})
source = urlopen(request, timeout=60).read()
exec(compile(source, URL, "exec"), globals(), globals())
