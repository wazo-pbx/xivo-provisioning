# $Revision$
# $Date$

include ../autobuild.mk

DEB_PKG="pf-xivo-provisioning"
DEB_TB_DEPS="python2.4-dev upx-ucl"
DEB_TAR_EXTRA_OPTIONS="--exclude=*.py"

DESTDIR?=.
FREEZEPATH?=../tools/python-freeze/

default:

prepare-tarball::
	@echo "from Phones import *" | PYTHONPATH=../lib-python/ /usr/bin/python2.4
	@echo "import provsup" | PYTHONPATH=../lib-python/ /usr/bin/python2.4
	@${FREEZEPATH}/local_freeze.py ${FREEZEPATH}/freeze.py provsup.py,autoprov.py
	@cp initconfig.py initconfig

clean-tarball::
	@find . ${FREEZEPATH} ../lib-python/ -name "*.pyc" -exec rm -f {} \;
	@rm -f autoprov initconfig

