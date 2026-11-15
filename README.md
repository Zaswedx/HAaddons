Homeassistant addons
====================
### 자세한 설명은 해당 폴더에서 확인하세요.

# 코맥스 월패드 애드온 (mqtt)
## CommaxWallpadBySaram
mqtt를 이용한 코맥스 월패드 애드온. ew11 같은 장비가 필요합니다.
- 리빌딩 (이라고 하지만 이전꺼와 거의 유사합니다.)
- 난방(Thermo)은 v1.2.0부터 Commax 클라우드 제어 + RS485 상태 하이브리드로 동작합니다. 아래 "난방(Thermo) 패킷" 참고.
- 엘리베이터는 제가 안쓰다 보니 테스트를 안합니다.


#
#### * 시리얼 통신으로 작동하는 앱은 링크로 이동 바랍니다.
https://github.com/HAKorea/addons


## 난방(Thermo) 패킷 분석
조명과 같은 하이브리드입니다. 제어는 Commax 클라우드(Ruvie API), 상태는 RS485 상태 패킷이며, 클라우드 실패 시 RS485 큐로 폴백합니다.

### RS485 (8바이트, 마지막 바이트 = 앞 7바이트 합 mod 256)
`방` = 방 번호(01~05), `BCD` = 온도를 십진 BCD로 표기(25°C → `25`).

| 종류 | 패킷 | 설명 |
|---|---|---|
| 명령 켜기 | `04 <방> 04 81 00 00 00 <sum>` | 예: 서재 `04 05 04 81 00 00 00 8E` |
| 명령 끄기 | `04 <방> 04 00 00 00 00 <sum>` | 예: `04 05 04 00 00 00 00 0D` |
| 명령 설정온도 | `04 <방> 03 <BCD> 00 00 00 <sum>` | 예: 25°C `04 05 03 25 00 00 00 31` |
| ACK | `84 <81/00> <방> <현재> <설정> 00 00 <sum>` | 명령 직후 응답 |
| 상태(주기) | `82 <81/00> <방> <현재BCD> <설정BCD> 00 00 <sum>` | `81`=heat, `00`=off |

- `cwbs_devinfo.json`의 `stateOFF` 템플릿은 byte1이 `80`이지만 실제 off 상태는 `00`입니다. 코드는 byte1의 하위 니블(`data[3]`)만 보므로 동작에는 영향이 없습니다.

### 클라우드 (Ruvie `api.ruvie.co.kr:4100`)
- `GET /v1/gateways/{gatewayNo}`의 `commaxDevice: boiler`, `rootDevice: thermostat`.
- sub: `airTemperature`(읽기), `thermostatMode`(`heat`/`off`), `thermostatSetpoint`(5~45), 거실만 `thermostatAwayMode`.
- 제어는 `POST /v1/command/` (`cgp` set, `rootUuid`/`subUuid`). `deviceNumber` 순서가 RS485 방 번호와 같습니다(1 거실, 2 안방, 3 하진이방, 4 게스트방, 5 서재).

### 설정 메모
- 기기 파일(`found_device_file`)에 `Thermo.Number=0`이 저장되어 있으면 난방이 등록되지 않습니다. 파일을 고치거나 옵션 `thermo_number`로 덮어씁니다.
- 버너 점화 여부는 클라우드/패킷 어디에도 없습니다(모드와 온도만).
