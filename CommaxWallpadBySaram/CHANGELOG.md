수정 사항 Changelog
==================

v 1.2.0
-------
1. 난방(Thermo) 하이브리드: 제어는 Commax 클라우드(thermostatMode/thermostatSetpoint), 상태는 RS485 상태 패킷(82..). 클라우드 실패 시 RS485 큐로 폴백
2. 보일러 매핑은 /share/cwbs_cloud_boilers.json (cloud deviceNumber == RS485 방 번호). cloud_light_enabled가 보일러에도 적용됨

v 1.1.11
--------
1. MQTT Discovery config 발행을 retain=True로 변경하여 HA 재시작 후 unavailable 문제 완화

v 1.1.10
--------
1. RS485 탐색(조명 Number) 기준으로 cloud 조명 매핑 순서를 맞추는 로직 반영

v 1.0.1
-------
1. paho-mqtt 버전 1.6.1로 변경

v 1.0.0
-------
1. 이전 버전의 방치 기간이 너무 길어서 새로 작성
