# Default values for user options
.PHONY: default
default: all

venv:
	rm -rf bin/ venv/
	python3.11 -m venv ./venv
	venv/bin/pip install --upgrade pip
	venv/bin/pip install --upgrade setuptools
	venv/bin/pip install --upgrade wheel

	touch venv

bin/pip: setup.py requirements.txt
	venv/bin/pip install -r ./requirements.txt

	mkdir -p bin

	ln -sf $(PWD)/venv/bin/pip ./bin/pip
	ln -sf $(PWD)/venv/bin/config ./bin/config
	ln -sf $(PWD)/venv/bin/discover ./bin/discover

	mkdir -p log
	mkdir -p var

all: venv bin/pip


.PHONY: clean
clean:
	rm -rf bin/ venv/

.PHONY: real-clean
real-clean:
	git clean -dfx
	rm -rf bin/ venv/

.PHONY: tags
tags:
	./venv-ctags venv/bin/python
