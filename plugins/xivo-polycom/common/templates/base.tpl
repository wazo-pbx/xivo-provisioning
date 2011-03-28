<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<!-- TODO check for VLAN priority and qos.ethernet.rtp.user_priority attribute
     and how it fit with our less specific config model. -->
<!-- FIXME usage of sip.lines.x.number in this template is against what the
     config (see config.py) says, i.e. that it should be use only for display. -->
<config

device.set="1"

{# VLAN settings -#}
device.net.vlanId.set="1"
{% if vlan -%}
device.net.vlanId="{{ vlan['id'] }}"
{% else -%}
device.net.vlanId=""
{% endif -%}

{# Syslog settings -#}
device.syslog.serverName.set="1"
device.syslog.transport.set="1"
device.syslog.renderLevel.set="1"
{% if syslog -%}
device.syslog.serverName="{{ syslog['ip'] }}"
device.syslog.transport="1"
device.syslog.renderLevel="{{ XX_syslog_level }}"
{% else -%}
device.syslog.serverName=""
device.syslog.transport="0"
device.syslog.renderLevel="1"
{% endif -%}

device.sec.SSL.certList.set="1"
{% if XX_custom_cert -%}
device.sec.SSL.certList="custom"
device.sec.SSL.customCert.set="1"
device.sec.SSL.customCert="{{ XX_custom_cert }}"
{% else -%}
device.sec.SSL.certList="default"
{% endif -%}

{% if sip['srtp_mode'] == 'disabled' -%}
sec.srtp.enable="0"
sec.srtp.offer="0"
sec.srtp.require="0"
{% elif sip['srtp_mode'] == 'preferred' -%}
sec.srtp.enable="1"
sec.srtp.offer="1"
sec.srtp.require="0"
{% elif sip['srtp_mode'] == 'required' -%}
sec.srtp.enable="1"
sec.srtp.offer="1"
sec.srtp.require="1"
{% endif -%}

{# NTP settings -#}
{% if ntp_server_ip -%}
tcpIpApp.sntp.address.overrideDHCP="1"
tcpIpApp.sntp.address="{{ ntp_server_ip }}"
{% else -%}
tcpIpApp.sntp.address.overrideDHCP="0"
tcpIpApp.sntp.address=""
{% endif -%}

{{ XX_timezone }}

lcl.ml.lang="{{ XX_language }}"

{% if sip['dtmf_mode'] == 'RTP-in-band' -%}
tone.dtmf.viaRtp="1"
tone.dtmf.rfc2833Control="0"
voIpProt.SIP.dtmfViaSignaling.rfc2976="0"
{% elif sip['dtmf_mode'] == 'RTP-out-of-band' -%}
tone.dtmf.viaRtp="1"
tone.dtmf.rfc2833Control="1"
voIpProt.SIP.dtmfViaSignaling.rfc2976="0"
{% elif sip['dtmf_mode'] == 'SIP-INFO' -%}
tone.dtmf.viaRtp="0"
voIpProt.SIP.dtmfViaSignaling.rfc2976="1"
{% endif -%}

{% for line_no, line in sip['lines'].iteritems() %}
reg.{{ line_no }}.server.1.address="{{ line['proxy_ip'] }}"
reg.{{ line_no }}.server.1.port=""
reg.{{ line_no }}.server.1.transport="{{ XX_sip_transport }}"
reg.{{ line_no }}.server.1.expires="3600"
reg.{{ line_no }}.server.1.register="1"
reg.{{ line_no }}.server.1.retryMaxCount="2"
{% if line['backup_proxy_ip'] -%}
reg.{{ line_no }}.server.2.address="{{ line['backup_proxy_ip'] }}"
reg.{{ line_no }}.server.2.port=""
reg.{{ line_no }}.server.2.transport="{{ XX_sip_transport }}"
reg.{{ line_no }}.server.2.expires="3600"
{% endif -%}
reg.{{ line_no }}.displayName="{{ line['display_name']|e }}"
reg.{{ line_no }}.label="{{ line['username']|e }}"
reg.{{ line_no }}.address="{{ line['username']|e }}"
reg.{{ line_no }}.auth.userId="{{ line['auth_username']|e }}"
reg.{{ line_no }}.auth.password="{{ line['password']|e }}"
{% if sip['subscribe_mwi'] -%}
msg.mwi.{{ line_no }}.subscribe="{{ line['number'] }}"
{% else -%}
msg.mwi.{{ line_no }}.subscribe=""
{% endif -%}
msg.mwi.{{ line_no }}.callBackMode="contact"
msg.mwi.{{ line_no }}.callBack="{{ line['voicemail'] or exten['voicemail'] }}"
{% endfor -%}

{% if exten['pickup_call'] -%}
call.directedCallPickupString="{{ exten['pickup_call'] }}"
{% endif -%}

{{ XX_fkeys }}

{% if X_xivo_phonebook_ip -%}
mb.main.home="http://{{ X_xivo_phonebook_ip }}/service/ipbx/web_services.php/phonebook/search/"
{% endif -%}

{% block model_specific_parameters -%}
{% endblock -%}

/>
