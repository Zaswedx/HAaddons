import paho.mqtt.client as mqtt
import json
import time
import asyncio
import telnetlib
import re
import uuid
import traceback
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

share_dir = '/share'

ELFIN_TOPIC = 'ew11'


def log(string):
    date = time.strftime('%Y-%m-%d %p %I:%M:%S', time.localtime(time.time()))
    print(f'[{date}] {string}')
    return


def checksum(input_hex):
    try:
        input_hex = input_hex[:14]
        s1 = sum([int(input_hex[val], 16) for val in range(0, 14, 2)])
        s2 = sum([int(input_hex[val + 1], 16) for val in range(0, 14, 2)])
        s1 = s1 + int(s2 // 16)
        s1 = s1 % 16
        s2 = s2 % 16
        return input_hex + format(s1, 'X') + format(s2, 'X')
    except:
        return None


class CloudLightController:
    def __init__(self, config, device_list=None):
        self.enabled = config.get('cloud_light_enabled', False)
        self.verbose = config.get('cloud_verbose_log', True)
        self.auth_url = config.get('cloud_auth_url', 'https://biz.ruvie.co.kr:4000/oauth/authorize')
        self.base_url = config.get('cloud_base_url', 'https://api.ruvie.co.kr:4100')
        self.command_endpoint = config.get('cloud_command_endpoint', '/v1/command/')
        self.gateways_endpoint = config.get('cloud_gateways_endpoint', '/v1/gateways')
        self.gateway_detail_endpoint = config.get('cloud_gateway_detail_endpoint', '/v1/gateways/{gateway_no}')
        self.client_id = config.get('cloud_client_id', '')
        self.client_secret = config.get('cloud_client_secret', '')
        self.username = config.get('cloud_username', '')
        self.password = config.get('cloud_password', '')
        self.device_uuid = config.get('cloud_dvc_uuid', '')
        self.device_os = config.get('cloud_dvc_os', 'AND')
        self.app_version = config.get('cloud_app_version', '0.0.0.1')
        self.timeout = int(config.get('cloud_timeout', 10))
        self.refresh_margin = int(config.get('cloud_refresh_margin', 60))

        self.token_path = share_dir + '/cwbs_cloud_token.json'
        self.mapping_path = share_dir + '/cwbs_cloud_lights.json'
        self.client_path = share_dir + '/cwbs_cloud_client.json'
        self._token = None
        self._mapping = {}
        self._device_list = device_list or {}
        self._rs485_light_count = self._extract_rs485_light_count(self._device_list)

    async def initialize(self):
        if not self.enabled:
            log('[CLOUD] disabled (cloud_light_enabled=false)')
            return
        self._vlog('initialize start')
        await self._ensure_device_uuid()
        await self._load_token()
        if self._token:
            self._vlog('loaded cached token')
        else:
            self._vlog('no cached token')
        await self._load_mapping()
        self._vlog(f'loaded mapping size={len(self._mapping)}')
        if not self._mapping or self._needs_remap():
            await self.discover_lights()
        self._vlog('initialize done')

    async def set_light(self, idx, onoff):
        if not self.enabled:
            log(f'[CLOUD] set_light skipped (disabled): Light{idx} {onoff}')
            return False
        self._vlog(f'set_light start: idx={idx} onoff={onoff}')
        if not await self.ensure_token():
            log(f'[CLOUD] set_light aborted: ensure_token failed (Light{idx})')
            return False
        if not self._mapping:
            self._vlog('mapping empty; trying discover_lights()')
            await self.discover_lights()
        light = self._mapping.get(str(idx))
        if not light:
            log(f'[CLOUD] light mapping not found for Light{idx}')
            self._vlog(f'available mapping keys: {list(self._mapping.keys())[:20]}')
            return False
        self._vlog(
            f"mapped Light{idx} -> gateway={light.get('gateway_no')} "
            f"root={light.get('root_uuid')} sub={light.get('sub_uuid')}"
        )

        payload = {
            "commands": {
                "cgpCommand": [
                    {
                        "gatewayNo": light['gateway_no'],
                        "cgp": {
                            "command": "set",
                            "object": {
                                "rootUuid": light['root_uuid'],
                                "rootDevice": light.get('root_device', 'switch'),
                                "subDevice": [
                                    {
                                        "value": "on" if onoff == 'ON' else "off",
                                        "funcCommand": light.get('func_command', 'report'),
                                        "sort": light.get('sort', 'switchBinary'),
                                        "subUuid": light['sub_uuid'],
                                        "type": light.get('type', 'readWrite'),
                                    }
                                ],
                            },
                        },
                    }
                ]
            }
        }
        ok = await self._post_with_auth(self.command_endpoint, payload)
        if ok:
            log(f'[CLOUD] Light{idx} -> {onoff} success')
        else:
            log(f'[CLOUD] Light{idx} -> {onoff} failed')
        return ok

    async def discover_lights(self):
        if not self.enabled:
            return {}
        if not await self.ensure_token():
            log('[CLOUD] discover_lights failed: ensure_token failed')
            return {}

        self._vlog(f'discover_lights GET {self.gateways_endpoint}')
        gateways = await self._get_with_auth(self.gateways_endpoint)
        if not gateways:
            log('[CLOUD] discover_lights failed: gateways response empty')
            return {}

        items = gateways.get('gateways', {}).get('gateway', [])
        self._vlog(f'discover_lights gateways count={len(items)}')
        found = []
        for gw in items:
            gw_no = gw.get('gatewayNo')
            if not gw_no:
                continue
            self._vlog(f'discover_lights gateway detail: {gw_no}')
            detail = await self._get_with_auth(self.gateway_detail_endpoint.format(gateway_no=gw_no))
            if not detail:
                log(f'[CLOUD] gateway detail failed: {gw_no}')
                continue
            objects = detail.get('gateway', {}).get('devices', {}).get('object', [])
            for obj in objects:
                if obj.get('commaxDevice') != 'light':
                    continue
                sub = (obj.get('subDevice') or [{}])[0]
                root_uuid = obj.get('rootUuid')
                sub_uuid = sub.get('subUuid')
                if not root_uuid or not sub_uuid:
                    continue
                found.append({
                    'gateway_no': gw_no,
                    'root_uuid': root_uuid,
                    'sub_uuid': sub_uuid,
                    'root_device': obj.get('rootDevice', 'switch'),
                    'sort': sub.get('sort', 'switchBinary'),
                    'type': sub.get('type', 'readWrite'),
                    'func_command': sub.get('funcCommand', 'report'),
                    'nickname': obj.get('nickname', ''),
                    'device_number': str(obj.get('deviceNumber', '')),
                })

        found.sort(
            key=lambda x: (
                int(x.get('device_number')) if str(x.get('device_number', '')).isdigit() else 9999,
                x.get('nickname', ''),
            )
        )
        if self._rs485_light_count > 0:
            found = found[:self._rs485_light_count]
            self._vlog(f'apply RS485 light count: {self._rs485_light_count}')
        else:
            self._vlog('RS485 Light.Number not found or 0; fallback to all discovered cloud lights')
        mapping = {}
        for i, item in enumerate(found, start=1):
            mapping[str(i)] = item
        self._mapping = mapping
        await self._save_mapping()
        log(f'[CLOUD] discovered and mapped {len(mapping)} light(s)')
        if mapping:
            sample = {k: {'nickname': v.get('nickname', ''), 'device_number': v.get('device_number', '')}
                      for k, v in list(mapping.items())[:5]}
            self._vlog(f'mapping sample: {sample}')
            if self._rs485_light_count > 0 and len(mapping) < self._rs485_light_count:
                log(
                    f"[CLOUD] warning: RS485 Light.Number={self._rs485_light_count}, "
                    f"but cloud discovered {len(mapping)} light(s)"
                )
        return mapping

    def _extract_rs485_light_count(self, device_list):
        try:
            return int((device_list or {}).get('Light', {}).get('Number', 0))
        except Exception:
            return 0

    def _needs_remap(self):
        if self._rs485_light_count <= 0:
            return False
        if len(self._mapping) != self._rs485_light_count:
            self._vlog(
                f"mapping size mismatch; remap needed "
                f"(mapping={len(self._mapping)} rs485={self._rs485_light_count})"
            )
            return True
        expected_keys = {str(i) for i in range(1, self._rs485_light_count + 1)}
        actual_keys = set(self._mapping.keys())
        if actual_keys != expected_keys:
            self._vlog('mapping key mismatch; remap needed')
            return True
        return False

    async def ensure_token(self):
        if not self.enabled:
            return False
        now = int(time.time())
        if self._token and self._token.get('access_expires_at', 0) - self.refresh_margin > now:
            self._vlog('ensure_token: cached access token is valid')
            return True
        if self._token and self._token.get('refresh_token'):
            self._vlog('ensure_token: trying refresh token')
            if await self._refresh(self._token['refresh_token']):
                self._vlog('ensure_token: refresh success')
                return True
            log('[CLOUD] token refresh failed, fallback login')
        else:
            self._vlog('ensure_token: no refresh token, trying login')
        return await self._login()

    async def _login(self):
        params = {
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'grant_type': 'password',
            'username': self.username,
            'password': self.password,
        }
        self._vlog('login request start')
        data = await self._http_get(self.auth_url, params=params, headers=self._auth_headers())
        if not data:
            log('[CLOUD] login failed: empty response')
            return False
        return await self._apply_token(data)

    async def _refresh(self, refresh_token):
        params = {
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'grant_type': 'refresh_token',
            'refresh_token': refresh_token,
        }
        self._vlog('refresh request start')
        data = await self._http_get(self.auth_url, params=params, headers=self._auth_headers())
        if not data:
            log('[CLOUD] refresh failed: empty response')
            return False
        return await self._apply_token(data)

    async def _apply_token(self, data):
        access_token = data.get('access_token')
        refresh_token = data.get('refresh_token')
        expires_in = data.get('expires_in') or data.get('expire_in')
        if not access_token or not refresh_token or not expires_in:
            log('[CLOUD] token parse failed: missing token fields')
            return False
        try:
            expires_at = int(time.time()) + int(expires_in)
        except Exception:
            return False
        self._token = {
            'access_token': access_token,
            'refresh_token': refresh_token,
            'access_expires_at': expires_at,
        }
        await self._save_token()
        self._vlog(f'token applied: expires_at={expires_at}')
        return True

    def _auth_headers(self):
        return {
            'cmx-dvc-uuid': self.device_uuid,
            'cmx-dvc-os': self.device_os,
            'cmx-app-version': self.app_version,
        }

    def _join_url(self, path):
        return self.base_url.rstrip('/') + path

    async def _get_with_auth(self, path):
        if not self._token:
            self._vlog(f'GET {path} skipped: no token')
            return None
        headers = {'Authorization': f"Bearer {self._token['access_token']}"}
        self._vlog(f'GET request: {path}')
        data = await self._http_get(self._join_url(path), headers=headers)
        if data is not None:
            return data
        self._vlog(f'GET failed once; retry with login: {path}')
        if await self._login():
            headers = {'Authorization': f"Bearer {self._token['access_token']}"}
            return await self._http_get(self._join_url(path), headers=headers)
        return None

    async def _post_with_auth(self, path, payload):
        if not self._token:
            self._vlog(f'POST {path} skipped: no token')
            return False
        headers = {'Authorization': f"Bearer {self._token['access_token']}", 'Content-Type': 'application/json'}
        self._vlog(f'POST request: {path}')
        data = await self._http_post(self._join_url(path), payload=payload, headers=headers)
        if data is not None:
            return True
        self._vlog(f'POST failed once; retry with login: {path}')
        if await self._login():
            headers = {'Authorization': f"Bearer {self._token['access_token']}", 'Content-Type': 'application/json'}
            data = await self._http_post(self._join_url(path), payload=payload, headers=headers)
            return data is not None
        return False

    async def _http_get(self, url, params=None, headers=None):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self._http_get_sync(url, params, headers))

    async def _http_post(self, url, payload=None, headers=None):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self._http_post_sync(url, payload, headers))

    def _http_get_sync(self, url, params=None, headers=None):
        if params:
            query = urlencode(params)
            sep = '&' if '?' in url else '?'
            url = f'{url}{sep}{query}'
        req = Request(url=url, method='GET', headers=headers or {})
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode('utf-8')
                self._vlog(f'GET ok: {url} status={getattr(resp, "status", "na")}')
                return json.loads(raw) if raw else {}
        except HTTPError as err:
            body = ''
            try:
                body = err.read().decode('utf-8')
            except Exception:
                pass
            log(f'[CLOUD] GET HTTPError: {url} status={err.code} body={body[:300]}')
            return None
        except (URLError, TimeoutError, ValueError, OSError) as err:
            log(f'[CLOUD] GET failed: {url} ({err})')
            return None

    def _http_post_sync(self, url, payload=None, headers=None):
        body = json.dumps(payload or {}).encode('utf-8')
        req = Request(url=url, data=body, method='POST', headers=headers or {})
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode('utf-8')
                self._vlog(f'POST ok: {url} status={getattr(resp, "status", "na")}')
                return json.loads(raw) if raw else {}
        except HTTPError as err:
            body = ''
            try:
                body = err.read().decode('utf-8')
            except Exception:
                pass
            log(f'[CLOUD] POST HTTPError: {url} status={err.code} body={body[:300]}')
            return None
        except (URLError, TimeoutError, ValueError, OSError) as err:
            log(f'[CLOUD] POST failed: {url} ({err})')
            return None

    async def _load_token(self):
        loop = asyncio.get_running_loop()
        self._token = await loop.run_in_executor(None, self._load_json_file, self.token_path, None)

    async def _save_token(self):
        if not self._token:
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._save_json_file, self.token_path, self._token)

    async def _load_mapping(self):
        loop = asyncio.get_running_loop()
        self._mapping = await loop.run_in_executor(None, self._load_json_file, self.mapping_path, {})

    async def _save_mapping(self):
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._save_json_file, self.mapping_path, self._mapping)

    async def _ensure_device_uuid(self):
        if self.device_uuid:
            self._vlog(f'use configured dvc_uuid: {self.device_uuid}')
            return
        loop = asyncio.get_running_loop()
        meta = await loop.run_in_executor(None, self._load_json_file, self.client_path, {})
        dvc_uuid = meta.get('dvc_uuid')
        if not dvc_uuid:
            dvc_uuid = str(uuid.uuid4()).upper()
            meta['dvc_uuid'] = dvc_uuid
            await loop.run_in_executor(None, self._save_json_file, self.client_path, meta)
            log(f'[CLOUD] generated dvc_uuid: {dvc_uuid}')
        self.device_uuid = dvc_uuid
        self._vlog(f'use generated dvc_uuid: {dvc_uuid}')

    def _vlog(self, message):
        if self.verbose:
            log(f'[CLOUD][VERBOSE] {message}')

    @staticmethod
    def _load_json_file(path, default):
        try:
            with open(path, 'r', encoding='utf-8') as file:
                data = json.load(file)
                if isinstance(data, dict):
                    return data
                return default
        except Exception:
            return default

    @staticmethod
    def _save_json_file(path, data):
        with open(path, 'w', encoding='utf-8') as file:
            json.dump(data, file, ensure_ascii=True, indent=2)


def find_device(config):
    HA_TOPIC = config['mqtt_TOPIC']
    found_device_file = config.get('found_device_file', 'cwbs_found_device.json')

    with open('/apps/cwbs_devinfo.json') as file:
        dev_info = json.load(file)
    statePrefix = {dev_info[name]['stateON'][:2]: name for name in dev_info if dev_info[name].get('stateON')}
    device_num = {name: 0 for name in statePrefix.values()}
    collect_data = {name: set() for name in statePrefix.values()}

    target_time = time.time() + 20

    def on_connect(client, userdata, flags, rc):
        if rc == 0:
            log("MQTT broker 접속 완료")
            log("20초동안 기기를 검색합니다.")
            client.subscribe(f'{ELFIN_TOPIC}/#', 0)
        else:
            errcode = {1: 'Connection refused - incorrect protocol version',
                       2: 'Connection refused - invalid client identifier',
                       3: 'Connection refused - server unavailable',
                       4: 'Connection refused - bad username or password',
                       5: 'Connection refused - not authorised'}
            log(errcode[rc])

    def on_message(client, userdata, msg):
        raw_data = msg.payload.hex().upper()
        for k in range(0, len(raw_data), 16):
            data = raw_data[k:k + 16]
            # log(data)
            if data == checksum(data) and data[:2] in statePrefix:
                name = statePrefix[data[:2]]
                collect_data[name].add(data)
                if dev_info[name].get('stateNUM'):
                    device_num[name] = max([device_num[name], int(data[int(dev_info[name]['stateNUM']) - 1])])
                else:
                    device_num[name] = 1

    mqtt_client = mqtt.Client('cwbs')
    mqtt_client.username_pw_set(config['mqtt_id'], config['mqtt_password'])
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message
    mqtt_client.connect_async(config['mqtt_server'])
    mqtt_client.user_data_set(target_time)
    mqtt_client.loop_start()

    while time.time() < target_time:
        pass

    mqtt_client.loop_stop()

    log('다음의 데이터를 찾았습니다...')
    log('======================================')

    for name in collect_data:
        collect_data[name] = sorted(collect_data[name])
        dev_info[name]['Number'] = device_num[name]
        log('DEVICE: {}'.format(name))
        log('Packets: {}'.format(collect_data[name]))
        log('-------------------')
    log('======================================')
    log('기기의 숫자만 변경하였습니다. 상태 패킷은 직접 수정하여야 합니다.')
    save_path = f'{share_dir}/{found_device_file}'
    with open(save_path, 'w', encoding='utf-8') as make_file:
        json.dump(dev_info, make_file, indent="\t")
        log(f'기기리스트 저장 중 : {save_path}')
        log('파일을 수정하고 싶은 경우 종료 후 다시 시작하세요.')
    return dev_info


def do_work(config, device_list):
    HA_TOPIC = config['mqtt_TOPIC']
    STATE_TOPIC = HA_TOPIC + '/{}/{}/state'
    DISCOVERY_PREFIX = config.get('discovery_prefix', 'cwbs')

    debug = config['DEBUG']
    mqtt_log = config['mqtt_log']
    elfin_log = config['elfin_log']

    mqtt_client_id = config.get('mqtt_client_id', f'{HA_TOPIC}_client')
    mqtt_client = mqtt.Client(mqtt_client_id)
    cloud_light = CloudLightController(config, device_list)

    def pad(value):
        value = int(value)
        return '0' + str(value) if value < 10 else str(value)

    def make_hex(k, input_hex, change):
        if input_hex:
            try:
                change = int(change)
                input_hex = f'{input_hex[:change - 1]}{int(input_hex[change - 1]) + k}{input_hex[change:]}'
            except:
                pass
        return checksum(input_hex)

    def make_hex_temp(k, curTemp, setTemp, state):  # 온도조절기 16자리 (8byte) hex 만들기
        if state == 'OFF' or state == 'ON' or state == 'CHANGE':
            tmp_hex = device_list['Thermo'].get('command' + state)
            change = device_list['Thermo'].get('commandNUM')
            tmp_hex = make_hex(k, tmp_hex, change)
            if state == 'CHANGE':
                setT = pad(setTemp)
                chaTnum = OPTION['Thermo'].get('chaTemp')
                tmp_hex = tmp_hex[:chaTnum - 1] + setT + tmp_hex[chaTnum + 1:]
            return checksum(tmp_hex)
        else:
            tmp_hex = device_list['Thermo'].get(state)
            change = device_list['Thermo'].get('stateNUM')
            tmp_hex = make_hex(k, tmp_hex, change)
            setT = pad(setTemp)
            curT = pad(curTemp)
            curTnum = OPTION['Thermo'].get('curTemp')
            setTnum = OPTION['Thermo'].get('setTemp')
            tmp_hex = tmp_hex[:setTnum - 1] + setT + tmp_hex[setTnum + 1:]
            tmp_hex = tmp_hex[:curTnum - 1] + curT + tmp_hex[curTnum + 1:]
            if state == 'stateOFF':
                return checksum(tmp_hex)
            elif state == 'stateON':
                tmp_hex2 = tmp_hex[:3] + str(3) + tmp_hex[4:]
                return [checksum(tmp_hex), checksum(tmp_hex2)]
            else:
                return None

    def make_device_info(dev_name):
        num = device_list[dev_name].get('Number', 0)
        if num > 0:
            arr = [ {cmd + onoff: make_hex(k, device_list[dev_name].get(cmd + onoff), device_list[dev_name].get(cmd + 'NUM'))
                           for cmd in ['command', 'state'] for onoff in ['ON', 'OFF']} for k in range(num) ]
            if dev_name == 'fan':
                tmp_hex = arr[0]['stateON']
                change = device_list[dev_name].get('speedNUM')
                arr[0]['stateON'] = [make_hex(k, tmp_hex, change) for k in range(3)]
                tmp_hex = device_list[dev_name].get('commandCHANGE')
                arr[0]['CHANGE'] = [make_hex(k, tmp_hex, change) for k in range(3)]

            return {'type': device_list[dev_name]['type'], 'list': arr}
        else:
            return None

    DEVICE_LISTS = {}
    for name in device_list:
        device_info = make_device_info(name)
        if device_info:
            DEVICE_LISTS[name] = device_info

    prefix_list = {}
    log('----------------------')
    log('등록된 기기 목록 DEVICE_LISTS..')
    log('----------------------')
    for name in DEVICE_LISTS:
        state = DEVICE_LISTS[name]['list'][0].get('stateON')
        if state:
            prefix = state[0][:2] if isinstance(state, list) else state[:2]
            prefix_list[prefix] = name
        log(f'{name}: {DEVICE_LISTS[name]["type"]}')
        log(f' >>>> {DEVICE_LISTS[name]["list"]}')
    log('----------------------')

    HOMESTATE = {}
    QUEUE = []
    COLLECTDATA = {'data': set(), 'EVtime': time.time(), 'LastRecv': time.time_ns()}

    async def recv_from_HA(topics, value):
        if mqtt_log:
            log('[LOG] HA ->> : {} -> {}'.format('/'.join(topics), value))
        elif topics[0] == HA_TOPIC and topics[-1] == 'command':
            log(f'[CMD] {"/".join(topics)} -> {value}')

        device = ''.join(re.findall('[a-zA-Z]', topics[1]))

        if device in DEVICE_LISTS:
            key = topics[1] + topics[2]
            idx = int(''.join(re.findall(r'\d', topics[1])))
            value = 'ON' if value == 'heat' else value.upper()

            if device == 'Light' and topics[2] == 'power' and value in ['ON', 'OFF']:
                log(f'[CLOUD] intercepted Light command: device={topics[1]} value={value}')
                ok = await cloud_light.set_light(idx, value)
                if ok:
                    await update_state(device, idx - 1, value)
                    return
                log(f'[CLOUD] fallback to RS485 queue for {topics[1]}')

            if device == 'Thermo':
                curTemp = HOMESTATE.get(topics[1] + 'curTemp')
                setTemp = HOMESTATE.get(topics[1] + 'setTemp')
                if topics[2] == 'power':
                    sendcmd = make_hex_temp(idx - 1, curTemp, setTemp, value)
                    recvcmd = [make_hex_temp(idx - 1, curTemp, setTemp, 'state' + value)]
                    if sendcmd:
                        QUEUE.append({'sendcmd': sendcmd, 'recvcmd': recvcmd, 'count': 0})
                        if debug:
                            log('[DEBUG] Queued ::: sendcmd: {}, recvcmd: {}'.format(sendcmd, recvcmd))
                elif topics[2] == 'setTemp':
                    value = int(float(value))
                    if value == int(setTemp):
                        if debug:
                            log('[DEBUG] {} is already set: {}'.format(topics[1], value))
                    else:
                        setTemp = value
                        sendcmd = make_hex_temp(idx - 1, curTemp, setTemp, 'CHANGE')
                        recvcmd = [make_hex_temp(idx - 1, curTemp, setTemp, 'stateON')]
                        if sendcmd:
                            QUEUE.append({'sendcmd': sendcmd, 'recvcmd': recvcmd, 'count': 0})
                            if debug:
                                log('[DEBUG] Queued ::: sendcmd: {}, recvcmd: {}'.format(sendcmd, recvcmd))

            elif device == 'Fan':
                if topics[2] == 'power':
                    sendcmd = DEVICE_LISTS[device]['list'][idx-1].get('command' + value)
                    recvcmd = DEVICE_LISTS[device]['list'][idx-1].get('state' + value) if value == 'ON' else [
                        DEVICE_LISTS[device]['list'][idx-1].get('state' + value)]
                    QUEUE.append({'sendcmd': sendcmd, 'recvcmd': recvcmd, 'count': 0})
                    if debug:
                        log('[DEBUG] Queued ::: sendcmd: {}, recvcmd: {}'.format(sendcmd, recvcmd))
                elif topics[2] == 'speed':
                    speed_list = ['LOW', 'MEDIUM', 'HIGH']
                    if value in speed_list:
                        index = speed_list.index(value)
                        sendcmd = DEVICE_LISTS[device]['list'][idx-1]['CHANGE'][index]
                        recvcmd = [DEVICE_LISTS[device]['list'][idx-1]['stateON'][index]]
                        QUEUE.append({'sendcmd': sendcmd, 'recvcmd': recvcmd, 'count': 0})
                        if debug:
                            log('[DEBUG] Queued ::: sendcmd: {}, recvcmd: {}'.format(sendcmd, recvcmd))

            else:
                sendcmd = DEVICE_LISTS[device]['list'][idx-1].get('command' + value)
                if sendcmd:
                    recvcmd = [DEVICE_LISTS[device]['list'][idx-1].get('state' + value, 'NULL')]
                    QUEUE.append({'sendcmd': sendcmd, 'recvcmd': recvcmd, 'count': 0})
                    if debug:
                        log('[DEBUG] Queued ::: sendcmd: {}, recvcmd: {}'.format(sendcmd, recvcmd))
                else:
                    if debug:
                        log('[DEBUG] There is no command for {}'.format('/'.join(topics)))
        else:
            if debug:
                log('[DEBUG] There is no command for {}'.format('/'.join(topics)))

    async def slice_raw_data(raw_data):
        if elfin_log:
            log('[SIGNAL] receved: {}'.format(raw_data))

        packets = [
            raw_data[k:k + 16]
            for k in range(0, len(raw_data), 16)
            if raw_data[k:k + 16] == checksum(raw_data[k:k + 16])
        ]
        cors = [recv_from_elfin(packet) for packet in packets]
        results = await asyncio.gather(*cors, return_exceptions=True)
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                if debug:
                    log(
                        f"[DEBUG] recv_from_elfin error: packet={packets[i]} "
                        f"type={type(result).__name__} repr={result!r}"
                    )

    async def recv_from_elfin(data):
        COLLECTDATA['LastRecv'] = time.time_ns()
        if data:
            if HOMESTATE.get('EV1power') == 'ON':
                if COLLECTDATA['EVtime'] < time.time():
                    await update_state('EV', 0, 'OFF')
            for que in QUEUE:
                if data in que['recvcmd']:
                    QUEUE.remove(que)
                    if debug:
                        log('[DEBUG] Found matched hex: {}. Delete a queue: {}'.format(data, que))
                    break

            device_name = prefix_list.get(data[:2])
            if not device_name:
                if debug:
                    log(f"[DEBUG] Unknown packet prefix: {data[:2]} raw={data}")
                return
            if device_name == 'Thermo':
                curTnum = device_list['Thermo']['curTemp']
                setTnum = device_list['Thermo']['setTemp']
                curT = data[curTnum - 1:curTnum + 1]
                setT = data[setTnum - 1:setTnum + 1]
                onoffNUM = device_list['Thermo']['stateONOFFNUM']
                staNUM = device_list['Thermo']['stateNUM']
                index = int(data[staNUM - 1]) - 1
                onoff = 'ON' if int(data[onoffNUM - 1]) > 0 else 'OFF'
                await update_state(device_name, index, onoff)
                await update_temperature(index, curT, setT)
            elif device_name == 'Fan':
                if data in DEVICE_LISTS['Fan']['list'][0]['stateON']:
                    speed = DEVICE_LISTS['Fan']['list'][0]['stateON'].index(data)
                    await update_state('Fan', 0, 'ON')
                    await update_fan(0, speed)
                elif data == DEVICE_LISTS['Fan']['list'][0]['stateOFF']:
                    await update_state('Fan', 0, 'OFF')
                else:
                    log(f"[WARNING] <{device_name}> 기기의 신호를 찾음: {data}")
                    log('[WARNING] 기기목록에 등록되지 않는 패킷입니다. JSON 파일을 확인하세요..')
            elif device_name == 'Outlet':
                staNUM = device_list['Outlet']['stateNUM']
                index = int(data[staNUM - 1]) - 1

                for onoff in ['OFF', 'ON']:
                    if data.startswith(DEVICE_LISTS[device_name]['list'][index]['state' + onoff][:8]):
                        await update_state(device_name, index, onoff)
                        if onoff == 'ON':
                            await update_outlet_value(index, data[10:14])
                        else:
                            await update_outlet_value(index, 0)
            elif device_name == 'EV':
                val = int(data[4:6], 16)
                await update_state('EV', 0, 'ON')
                await update_ev_value(0, val)
                COLLECTDATA['EVtime'] = time.time() + 3
            else:
                num = len(DEVICE_LISTS[device_name]['list'])
                state = [DEVICE_LISTS[device_name]['list'][k]['stateOFF'] for k in range(num)] + [
                    DEVICE_LISTS[device_name]['list'][k]['stateON'] for k in range(num)]
                if data in state:
                    index = state.index(data)
                    onoff, index = ['OFF', index] if index < num else ['ON', index - num]
                    await update_state(device_name, index, onoff)
                else:
                    log(f"[WARNING] <{device_name}> 기기의 신호를 찾음: {data}")
                    log('[WARNING] 기기목록에 등록되지 않는 패킷입니다. JSON 파일을 확인하세요..')

    async def update_state(device, idx, onoff):
        state = 'power'
        deviceID = device + str(idx + 1)
        key = deviceID + state

        topic = STATE_TOPIC.format(deviceID, state)
        mqtt_client.publish(topic, onoff.encode())
        if mqtt_log:
            log('[LOG] ->> HA : {} >> {}'.format(topic, onoff))
        return
#
    async def update_fan(idx, onoff):
        deviceID = 'Fan' + str(idx + 1)
        if onoff == 'ON' or onoff == 'OFF':
            state = 'power'
        else:
            try:
                speed_list = ['low', 'medium', 'high']
                onoff = speed_list[int(onoff) - 1]
                state = 'speed'
            except:
                return
        key = deviceID + state
        topic = STATE_TOPIC.format(deviceID, state)
        mqtt_client.publish(topic, onoff.encode())
        if mqtt_log:
            log('[LOG] ->> HA : {} >> {}'.format(topic, onoff))
        return

    async def update_temperature(idx, curTemp, setTemp):
        deviceID = 'Thermo' + str(idx + 1)
        temperature = {'curTemp': pad(curTemp), 'setTemp': pad(setTemp)}
        for state in temperature:
            key = deviceID + state
            val = temperature[state]
            topic = STATE_TOPIC.format(deviceID, state)
            mqtt_client.publish(topic, val.encode())
            HOMESTATE[deviceID + 'curTemp'] = curTemp
            HOMESTATE[deviceID + 'setTemp'] = setTemp
            if mqtt_log:
                log('[LOG] ->> HA : {} -> {}'.format(topic, val))
        return

    async def update_outlet_value(idx, val):
        deviceID = 'Outlet' + str(idx + 1)
        try:
            val = '%.1f' % float(int(val) / 10)
            topic = STATE_TOPIC.format(deviceID, 'watt')
            mqtt_client.publish(topic, val.encode())
            if debug:
                log('[LOG] ->> HA : {} -> {}'.format(topic, val))
        except:
            pass

    async def update_ev_value(idx, val):
        deviceID = 'EV' + str(idx + 1)
        try:
            BF = device_info['EV']['BasementFloor']
            val = str(int(val) - BF + 1) if val >= BF else 'B' + str(BF - int(val))
            topic = STATE_TOPIC.format(deviceID, 'floor')
            mqtt_client.publish(topic, val.encode())
            if debug:
                log('[LOG] ->> HA : {} -> {}'.format(topic, val))
        except:
            pass

    def on_connect(client, userdata, flags, rc):
        if rc == 0:
            log("MQTT 접속 완료..")
            client.subscribe([(HA_TOPIC + '/#', 0), (ELFIN_TOPIC + '/recv', 0), (ELFIN_TOPIC + '/send', 1)])
            log(f"[MQTT] subscribed: {HA_TOPIC}/#, {ELFIN_TOPIC}/recv, {ELFIN_TOPIC}/send")
            log(f"[CLOUD] enabled={config.get('cloud_light_enabled')} topic={HA_TOPIC}")
            if 'EV' in DEVICE_LISTS:
                asyncio.run(update_state('EV', 0, 'OFF'))
            # MQTT discover
            for device in DEVICE_LISTS:
                for idx in range(len(DEVICE_LISTS[device]['list'])):
                    config_topic = (
                        f'homeassistant/{DEVICE_LISTS[device]["type"]}/'
                        f'{DISCOVERY_PREFIX}_{device.lower()}{idx + 1}/config'
                    )
                    if DEVICE_LISTS[device]["type"] == "climate":
                        payload = {
                            "device": {
                                "identifiers": DISCOVERY_PREFIX,
                                "name": "코맥스 월패드 by Zaswedx",
                                "manufacturer": "commax",
                            },
                            "device_class": DEVICE_LISTS[device]["type"],
                            "name": f'{device}{idx+1}',
                            "object_id": f'{DISCOVERY_PREFIX}_{device.lower()}{idx + 1}',
                            "unique_id": f'{DISCOVERY_PREFIX}_{device.lower()}{idx + 1}',
                            "entity_category": 'config',
                            "mode_cmd_t": f"{HA_TOPIC}/{device}{idx+1}/power/command",
                            "mode_stat_t": f"{HA_TOPIC}/{device}{idx+1}/power/state",
                            "temp_cmd_t": f"{HA_TOPIC}/{device}{idx+1}/setTemp/command",
                            "temp_stat_t": f"{HA_TOPIC}/{device}{idx+1}/setTemp/state",
                            "curr_temp_t": f"{HA_TOPIC}/{device}{idx+1}/curTemp/state",
                            "min_temp":"10",
                            "max_temp":"30",
                            "temp_step":"1",
                            "modes":["off", "heat"],
                            "mode_state_template": "{% set modes = {'OFF': 'off', 'ON': 'heat'} %} {{modes[value] if value in modes.keys() else 'off'}}"
                                }
                    else:
                        if DEVICE_LISTS[device]["type"] == "button":
                            press_payload = "OFF" if device == "Gas" else "ON"
                            payload = {
                                "device": {
                                    "identifiers": DISCOVERY_PREFIX,
                                    "name": "코맥스 월패드 by Zaswedx",
                                    "manufacturer": "commax",
                                },
                                "name": f'{device}{idx+1}',
                                "object_id": f'{DISCOVERY_PREFIX}_{device.lower()}{idx + 1}',
                                "unique_id": f'{DISCOVERY_PREFIX}_{device.lower()}{idx + 1}',
                                "command_topic": f"{HA_TOPIC}/{device}{idx + 1}/power/command",
                                "payload_press": press_payload
                            }
                        else:
                            payload = {
                                "device": {
                                    "identifiers": DISCOVERY_PREFIX,
                                    "name": "코맥스 월패드 by Zaswedx",
                                    "manufacturer": "commax",
                                },
                                "~": f'{HA_TOPIC}/{device}{idx + 1}/power',
                                "device_class": DEVICE_LISTS[device]["type"],
                                "name": f'{device}{idx+1}',
                                "object_id": f'{DISCOVERY_PREFIX}_{device.lower()}{idx + 1}',
                                "unique_id": f'{DISCOVERY_PREFIX}_{device.lower()}{idx + 1}',
                                "cmd_t": "~/command",
                                "stat_t": "~/state"}
                            if device == "Outlet":
                                payload["device_class"] = 'outlet'
                                payload["entity_category"] = 'diagnostic'

                    log(config_topic)
                    log(json.dumps(payload))
                    mqtt_client.publish(config_topic, json.dumps(payload), retain=True)
                    if device == "Outlet":
                        config_topic = (
                            f'homeassistant/sensor/{DISCOVERY_PREFIX}_{device.lower()}{idx + 1}_watt/config'
                        )
                        payload = {
                            "device": {
                                "identifiers": DISCOVERY_PREFIX,
                                "name": "코맥스 월패드 by Zaswedx",
                                "manufacturer": "commax",
                            },
                            "device_class": 'energy',
                            "name": f'{device}{idx + 1} Watt',
                            "object_id": f'{DISCOVERY_PREFIX}_{device.lower()}{idx + 1}_watt',
                            "unique_id": f'{DISCOVERY_PREFIX}_{device.lower()}{idx + 1}_watt',
                            "entity_category": 'diagnostic',
                            "stat_t": f'{HA_TOPIC}/{device}{idx + 1}/watt/state',
                            "unit_of_measurement": "W"
                        }
                        log(config_topic)
                        log(json.dumps(payload))
                        mqtt_client.publish(config_topic, json.dumps(payload), retain=True)

        else:
            errcode = {1: 'Connection refused - incorrect protocol version',
                       2: 'Connection refused - invalid client identifier',
                       3: 'Connection refused - server unavailable',
                       4: 'Connection refused - bad username or password',
                       5: 'Connection refused - not authorised'}
            log(errcode[rc])

    def on_message(client, userdata, msg):
        topics = msg.topic.split('/')
        try:
            if topics[0] == HA_TOPIC and topics[-1] == 'command':
                payload = msg.payload.decode('utf-8')
                log(f"[MQTT] command received: topic={msg.topic} payload={payload}")
                asyncio.run(recv_from_HA(topics, payload))
            elif topics[0] == ELFIN_TOPIC and topics[-1] == 'recv':
                asyncio.run(slice_raw_data(msg.payload.hex().upper()))
        except Exception as err:
            log(
                f"[ERROR] on_message failed: topic={msg.topic} "
                f"type={type(err).__name__} repr={err!r}"
            )
            if debug:
                log(traceback.format_exc())
            return

    async def send_to_elfin():
        while True:
            try:
                if time.time_ns() - COLLECTDATA['LastRecv'] > 10000000000:  # 10s
                    log('[WARNING] 10초간 신호를 받지 못했습니다. ew11 기기를 재시작합니다.')
                    try:
                        elfin_id = config['elfin_id']
                        elfin_password = config['elfin_password']
                        elfin_server = config['elfin_server']

                        ew11 = telnetlib.Telnet(elfin_server)

                        ew11.read_until(b"login:")
                        ew11.write(elfin_id.encode('utf-8') + b'\n')
                        ew11.read_until(b"password:")
                        ew11.write(elfin_password.encode('utf-8') + b'\n')
                        ew11.write('Restart'.encode('utf-8') + b'\n')

                        await asyncio.sleep(10)
                    except:
                        log('[WARNING] 기기 재시작 오류! 기기 상태를 확인하세요.')
                    COLLECTDATA['LastRecv'] = time.time_ns()
                elif time.time_ns() - COLLECTDATA['LastRecv'] > 100000000:
                    if QUEUE:
                        send_data = QUEUE.pop(0)
                        if elfin_log:
                            log('[SIGNAL] 신호 전송: {}'.format(send_data))
                        mqtt_client.publish(ELFIN_TOPIC + '/send', bytes.fromhex(send_data['sendcmd']))
                        # await asyncio.sleep(0.01)
                        if send_data['count'] < 5:
                            send_data['count'] = send_data['count'] + 1
                            QUEUE.append(send_data)
                        else:
                            if elfin_log:
                                log('[SIGNAL] Send over 5 times. Send Failure. Delete a queue: {}'.format(send_data))
            except Exception as err:
                log('[ERROR] send_to_elfin(): {}'.format(err))
                return True
            await asyncio.sleep(0.01)

    mqtt_client.username_pw_set(config['mqtt_id'], config['mqtt_password'])
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message
    mqtt_client.connect_async(config['mqtt_server'])
    # mqtt_client.user_data_set(target_time)
    mqtt_client.loop_start()

    loop = asyncio.get_event_loop()
    if config.get('cloud_light_enabled'):
        try:
            loop.run_until_complete(cloud_light.initialize())
            log('[CLOUD] controller initialized')
        except Exception as err:
            log(f'[CLOUD] initialize error: {err}')
    loop.run_until_complete(send_to_elfin())
    loop.close()
    mqtt_client.loop_stop()


if __name__ == '__main__':
    log("'Commax Wallpad by Zaswedx'을 시작합니다.")
    with open('/data/options.json') as file:
        CONFIG = json.load(file)
    found_device_file = CONFIG.get('found_device_file', 'cwbs_found_device.json')
    found_device_path = f'{share_dir}/{found_device_file}'
    try:
        with open(found_device_path) as file:
            log(f'기기 정보 파일을 찾음: {found_device_path}')
            OPTION = json.load(file)
    except IOError:
        log('기기 정보 파일이 없습니다. mqtt에 접속하여 기기를 찾습니다.')
        OPTION = find_device(CONFIG)

    while True:
        do_work(CONFIG, OPTION)
