import asyncio, os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'apps'))
import main

def boiler(n, name):
    subs = [{'sort': s, 'subUuid': f'{s}-{n}', 'funcCommand': 'report', 'type': 'readWrite'}
            for s in ('thermostatMode', 'thermostatSetpoint')]
    return {'commaxDevice': 'boiler', 'rootUuid': f'root-{n}', 'deviceNumber': str(n), 'nickname': name, 'subDevice': subs}

def make():
    main.share_dir = tempfile.mkdtemp()
    c = main.CloudLightController({'cloud_light_enabled': True}, {'Thermo': {'Number': 2}})
    sent = []
    async def tok(): return True
    async def get(path):
        if path == '/v1/gateways': return {'gateways': {'gateway': [{'gatewayNo': 'g1'}]}}
        return {'gateway': {'devices': {'object': [boiler(2, '안방'), {'commaxDevice': 'light'}, boiler(1, '거실')]}}}
    async def post(path, payload): sent.append(payload); return True
    c.ensure_token, c._get_with_auth, c._post_with_auth = tok, get, post
    return c, sent

def test_discover_orders_by_device_number():
    c, _ = make()
    m = asyncio.run(c.discover_boilers())
    assert [m[k]['nickname'] for k in ('1', '2')] == ['거실', '안방']

def test_set_boiler_payload_and_validation():
    c, sent = make()
    assert asyncio.run(c.set_boiler(2, 'thermostatMode', 'heat'))
    sub = sent[0]['commands']['cgpCommand'][0]['cgp']['object']['subDevice'][0]
    assert sub['value'] == 'heat' and sub['subUuid'] == 'thermostatMode-2'
    assert asyncio.run(c.set_boiler(1, 'thermostatSetpoint', '25'))
    for bad in [(1, 'thermostatSetpoint', '99'), (1, 'thermostatSetpoint', 'x'), (1, 'thermostatMode', 'cool'), (9, 'thermostatMode', 'off')]:
        assert not asyncio.run(c.set_boiler(*bad))
    assert len(sent) == 2
