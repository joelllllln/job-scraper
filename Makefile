.PHONY: install test discover collect verify rank weekly insights clean run setup render

## The three you actually need on your own machine:
run:               ## everything, once — collect, verify, rank, email
	python3 run.py

setup:             ## change the saved email settings
	python3 run.py --setup

render:            ## browser pass over firms whose careers pages need JavaScript
	python3 run.py --render

install:
	pip install -r requirements.txt
	python3 -m playwright install chromium || \
	  echo "playwright browser not installed — 'make render' will not work until it is"

test:
	python3 selftest.py

discover:          ## monthly — find each firm's ATS
	python3 sniff.py && python3 discover.py

collect:           ## pull every source
	python3 scrape.py; python3 workday.py; python3 feeds.py --all; \
	python3 boards.py --hours 192; python3 efc.py --limit 200

verify:
	python3 verify.py

rank:
	python3 score.py --new-only --record && python3 notify.py

weekly:            ## the whole thing
	./weekly.sh

insights:
	python3 insights.py

clean:             ## remove generated files, keep jobs.db
	rm -f report.html report.md scored.csv latest.csv
