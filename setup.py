#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright 2008-2019 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from setuptools import setup
from setuptools import find_packages

setup(
    name='wazo-provd',
    version='0.2',
    description='Wazo provisioning daemon',
    author='Wazo Authors',
    author_email='dev@wazo.community',
    url='http://wazo.community',
    license='GPLv3',

    packages=find_packages(exclude=['*.tests']) + ['twisted', 'twisted.plugins'],
    package_data={'provd': ['tzinform/tzdatax', 'rest/api/api.yml'],
                  'twisted': ['.noinit'],
                  'twisted.plugins': ['provd_plugins.py', '.noinit']}
)
