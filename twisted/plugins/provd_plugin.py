# -*- coding: utf-8 -*-
# Copyright 2013-2019 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

# Twisted Application Pluguin (tap) file

from twisted.internet import epollreactor
epollreactor.install()

from provd.main import ProvisioningServiceMaker

service_maker = ProvisioningServiceMaker()
