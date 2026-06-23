# am-I-audible build targets
.PHONY: install deb appimage test clean

# Source install (clone path): system deps + global `listen` + models.
install:
	./setup.sh

# Build the Debian/Ubuntu package into dist/.
deb:
	bash packaging/deb/build.sh

# Build the portable AppImage into dist/ (needs appimagetool).
appimage:
	bash packaging/appimage/build.sh

test:
	python3 -m unittest discover -s tests

clean:
	rm -rf dist build src/*.egg-info **/__pycache__
