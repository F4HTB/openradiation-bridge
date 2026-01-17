#!/usr/bin/env python3
import time, struct, sys, uuid, json, urllib.request, datetime
from pydbus import SystemBus
from gi.repository import GLib
import threading

DUREE_S = 300  # fenêtre de mesure (comme l’appli 300s)

OR_API_USERID = "ThisIsATest"
OR_API_USERPWD = "ThisIsTheUSRPASSWD"

OR_API_LAT, OR_API_LON = 0.0, 0.0
OR_API_KEY = "bde8ebc61cb089b8cc997dd7a0d0a434"  # "bde8ebc61cb089b8cc997dd7a0d0a434" clé de test du README
OR_API_SUBMIT_URL = "https://submit.openradiation.net/measurements"
OR_API_REPORTTYPE = "routine" # "test" pour un test réponse [API] 200 {"test":true}
OR_API_ENV = "countryside" # "city", "ontheroad", "plane" ou "inside"
OR_API_SOFTWARE = "openradiation_pythonbridge_v1"
OR_API_ALTITUDE = 0.0  # en mètres
 
CAL_FUNC_GROUND_STR = "0.000001*(cps-0.14)^3+0.0025*(cps-0.14)^2+0.39*(cps-0.14)" #groundLevel

MAC = "AA:AA:AA:AA:AA:AA"
UUID_TX = "beb5483e-36e1-4688-b7f5-ea07361b26a8"  # notifications (device -> host)
UUID_RX = "beb5483f-36e1-4688-b7f5-ea07361b26a8"  # write (host -> device)

# --- Mapping  ---

OUT = {
	0x01: "serial",			# string
	0x02: "version",		   # string
	0x03: "sensorType",		# string
	0x04: "supplyVoltage",	 # float32 (V)

	0x05: "count",			 # uint8 (compte instantané)
	0x06: "temperature_c",	 # float32 (°C)

	0x10: "tubeType",		  # string
	0x11: "nominalHV",		 # float32 (V)
	0x12: "hv_volts",		  # float32 (V)
	0x13: "pwm_duty",		  # float32 (0..1)
	0x14: "calibCoeff",		# float32 (µSv/h par CPS) — supposé absent chez toi

	0xD1: "debug_byte1",	   # uint8
	0xD2: "debug_byte2",	   # uint8
	0xE1: "debug_str1",		# string
	0xE2: "debug_str2",		# string
	0xF1: "debug_float1",	  # float32
	0xF2: "debug_float2",	  # float32,
}
FLOAT_KEYS = {0x04, 0x06, 0x11, 0x12, 0x13, 0x14, 0xF1, 0xF2}
BYTE_KEYS  = {0x05, 0xD1, 0xD2}
STR_KEYS   = {0x01, 0x02, 0x03, 0x10, 0xE1, 0xE2}

def decode_tlv(payload: bytes) -> dict:
	"""Décode une trame TLV OpenRadiation selon Protocole.h."""
	i, n, out = 0, len(payload), {}
	while i < n:
		t = payload[i]; i += 1
		# floats 4 octets LE
		if t in FLOAT_KEYS and i + 4 <= n:
			out[OUT.get(t, f"type_{t:02X}")] = struct.unpack_from("<f", payload, i)[0]
			i += 4
			continue
		# octet simple
		if t in BYTE_KEYS and i < n:
			out[OUT.get(t, f"type_{t:02X}")] = payload[i]
			i += 1
			continue
		# chaîne longueur-prefixée (1 octet) — sendLenString
		if t in STR_KEYS and i < n:
			L = payload[i]; i += 1
			if i + L <= n:
				out[OUT.get(t, f"type_{t:02X}")] = payload[i:i+L].decode("utf-8", "replace")
				i += L
				continue
			break  # longueur incohérente -> stop proprement
		# type inconnu ou reste incomplet -> on s'arrête
		break
	return out

TUBE_VOLTAGE_PROFILE = {
	"SBM-20": {"min": 340, "max": 400, "set": 380.0},
	"M4011":  {"min": 340, "max": 420, "set": 400.0},
	"STS-5":  {"min": 340, "max": 400, "set": 400.0},
}

def set_tube_voltage(rx, tube_type: str):
	prof = TUBE_VOLTAGE_PROFILE.get(tube_type)
	if not prof:
		return
	try:
		payload = bytearray([0x11]) + bytearray(struct.pack("<f", prof["set"]))
		rx.WriteValue(payload, {})
		print(f"[cmd] HT cible envoyée: {prof['set']} V pour {tube_type}")
	except Exception as e:
		print(f"[cmd err] set voltage: {e}")
	
def hv_ready_info():
	"""Retourne (ready, hv_actuelle, hv_min, tubeType)"""
	ttype = agg.get("apparatusTubeType")
	hv	= agg.get("hv_volts")
	if not ttype or hv is None:
		return False, hv, None, ttype
	prof = TUBE_VOLTAGE_PROFILE.get(ttype)
	if not prof:
		return False, hv, None, ttype
	return (hv >= prof["min"]), hv, prof["min"], ttype


def set_visual_hits(rx, on: bool):
	"""LED (suivi de coups) : on=True -> 0x00, off=False -> 0x01"""
	try:
		rx.WriteValue(bytearray([0x01, 0x00 if on else 0x01]), {})
		print(f"[cmd] Visual hits {'ON' if on else 'OFF'}")
	except Exception as e:
		print(f"[cmd err] visual: {e}")

def set_audio_hits(rx, on: bool):
	"""Buzzer (suivi de coups) : on=True -> 0x00, off=False -> 0x01"""
	try:
		rx.WriteValue(bytearray([0x02, 0x00 if on else 0x01]), {})
		print(f"[cmd] Audio hits {'ON' if on else 'OFF'}")
	except Exception as e:
		print(f"[cmd err] audio: {e}")

# --- helpers advertising → serial ------------------------------------------
import re

def _ascii_from_bytes(b: bytes) -> str | None:
    try:
        s = b.decode("ascii", "ignore").strip()
        return s if s else None
    except Exception:
        return None

def _digits_tail(s: str) -> str | None:
    m = re.search(r'(\d+)$', s)
    return m.group(1) if m else None

def derive_apparatus_id_from_name(name: str | None, mac: str) -> str:
    """
    Essaie d’extraire un ID lisible :
      - chiffres en fin de nom BLE (ex: 'OpengKIT72' -> '00072')
      - sinon fallback sur 6 derniers hex du MAC
    """
    tail = _digits_tail(name or "")
    if tail:
        return tail.zfill(5)
    return mac.replace(":", "")[-6:].upper()

def grab_serial_from_advertising(bus, adapter_path: str, mac: str, timeout=3.0) -> str | None:
    """
    Scrute brièvement les données d’advertising BlueZ pour tenter d’y lire
    un numéro de série ASCII. Retourne une chaîne de chiffres (zerofill à faire ailleurs)
    ou None si rien trouvé.
    """
    mngr = bus.get("org.bluez", "/")
    adapter = bus.get("org.bluez", adapter_path)

    # Démarre un scan court
    try:
        adapter.StartDiscovery()
    except Exception:
        pass

    dev_path = adapter_path + "/dev_" + mac.replace(":", "_")
    t0 = time.time()
    found = None

    while time.time() - t0 < timeout and not found:
        objs = mngr.GetManagedObjects()
        if dev_path in objs:
            dprops = objs[dev_path].get("org.bluez.Device1", {})

            # 1) ManufacturerData: {uint16: ay}
            mdata = dprops.get("ManufacturerData")
            if isinstance(mdata, dict):
                for _, arr in mdata.items():
                    s = _ascii_from_bytes(bytes(arr))
                    if s and (s.isdigit() or _digits_tail(s)):
                        found = s if s.isdigit() else _digits_tail(s)
                        break

            # 2) ServiceData: {uuid: ay}
            if not found:
                sdata = dprops.get("ServiceData")
                if isinstance(sdata, dict):
                    for _, arr in sdata.items():
                        s = _ascii_from_bytes(bytes(arr))
                        if s and (s.isdigit() or _digits_tail(s)):
                            found = s if s.isdigit() else _digits_tail(s)
                            break

            # 3) À défaut, chiffres en fin du nom BLE (Alias/Name)
            if not found:
                name = dprops.get("Alias") or dprops.get("Name")
                if isinstance(name, str):
                    tail = _digits_tail(name)
                    if tail:
                        found = tail

        time.sleep(0.2)

    # Stop le scan
    try:
        adapter.StopDiscovery()
    except Exception:
        pass

    return found


def resolve_apparatus_identity(bus, adapter_path: str, device, mac: str) -> tuple[str, str]:
    """
    Retourne (apparatusId, apparatusVersion).
    - apparatusId : via advertising (serial ASCII) si dispo, sinon dérivé du nom BLE / MAC
    - apparatusVersion : nom BLE (Alias/Name) si dispo, sinon 'Unknown'
    """
    # Nom BLE depuis BlueZ (peut déjà être présent avant connexion si scanné)
    try:
        name = getattr(device, "Alias", None) or getattr(device, "Name", None)
    except Exception:
        name = None

    # Essaie l’advertising pour un serial ASCII
    try:
        serial_from_adv = grab_serial_from_advertising(bus, adapter_path, mac, timeout=3.0)
    except Exception:
        serial_from_adv = None

    if serial_from_adv:
        app_id = serial_from_adv.zfill(5)
    else:
        app_id = derive_apparatus_id_from_name(name, mac)

    app_ver = name or "Unknown"
    return app_id, app_ver



# --- API Openradiation ---
agg = {
	"start_ts": None,
	"hits": 0,
	"calibCoeff": None,			 # µSv/h par CPS (si 0x14 absent, on mettra un fallback)
	"hv_volts": None,
	"temperature_c": None,
	"apparatusId": None,			# serial
	"apparatusVersion": None,	   # version
	"apparatusSensorType": None,	# sensorType
	"apparatusTubeType": None,	  # tubeType (clé pour fallback)
}

def _prune_nulls(d):
	"""Supprime les clés dont la valeur est None (inplace)."""
	return {k: v for k, v in d.items() if v is not None}

def looks_like_step(pkt: bytes) -> bool:
    return (len(pkt) >= 14
            and pkt[0] == 0x05
            and pkt[2] == 0x06
            and pkt[9] == 0x12)

def submit_measurement(value_uSv_h, start_ts, end_ts, hits_number):
	payload = {
		"apiKey": OR_API_KEY,
		"data": {
			"reportUuid": str(uuid.uuid4()),
			"latitude": float(OR_API_LAT),
			"longitude": float(OR_API_LON),
			"altitude": float(OR_API_ALTITUDE), 
			"value": float(value_uSv_h),
			"startTime": datetime.datetime.fromtimestamp(start_ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
			"endTime": datetime.datetime.fromtimestamp(end_ts,   datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
			"reportContext": OR_API_REPORTTYPE,
			"manualReporting": False,
			"organisationReporting": OR_API_SOFTWARE,
			"hitsNumber": int(hits_number),
			"calibrationFunction": CAL_FUNC_GROUND_STR, 
			"apparatusId": agg["apparatusId"],
			"apparatusVersion": agg["apparatusVersion"],
			"apparatusSensorType": agg["apparatusSensorType"],
			"apparatusTubeType": agg["apparatusTubeType"],
			"temperature": int(round(agg["temperature_c"])) if agg["temperature_c"] is not None else None,
			"userId": OR_API_USERID,
			"userPwd":  OR_API_USERPWD,
			"measurementEnvironment":  OR_API_ENV,
		}
	}
	
	print("[API payload raw]", payload)
	print(f"To view: https://request.openradiation.net/measurements/{payload['data']['reportUuid']}?apiKey={OR_API_KEY}")
	
	body = json.dumps(payload).encode("utf-8")
	req = urllib.request.Request(
		OR_API_SUBMIT_URL,
		data=body,
		headers={
			"Content-Type": "application/json",   # <-- au lieu de vnd.api+json
			"Accept": "application/json",		 # <-- idem
		},
		method="POST",
	)
	try:
		with urllib.request.urlopen(req, timeout=15) as resp:
			print("[API]", resp.status, resp.read().decode())
	except urllib.error.HTTPError as e:
		print(f"[API err] HTTP {e.code} — {e.read().decode(errors='replace')}")
	except urllib.error.URLError as e:
		print(f"[API err] URL — {e}")


def start_measurement():
	agg.update({"start_ts": time.time(), "hits": 0})
	print("⏱️  Début mesure pour", DUREE_S, "s")

def maybe_finish_measurement():
	hits_snapshot = agg["hits"]
	if agg["start_ts"] and (time.time() - agg["start_ts"] >= DUREE_S):
		duration = time.time() - agg["start_ts"]
		cps = hits_snapshot / duration if duration > 0 else 0.0

		# groundLevel: 0.000001*(cps-0.14)^3 + 0.0025*(cps-0.14)^2 + 0.39*(cps-0.14)
		x = cps - 0.14
		value_uSv_h = (0.000001 * (x ** 3)) + (0.0025 * (x ** 2)) + (0.39 * x)
		value_uSv_h = max(0.0, value_uSv_h)

		print(f"✅ Fin mesure [groundLevel]: hits={hits_snapshot} cps={cps:.3f} => {value_uSv_h:.3f} µSv/h")
		try:
			
			end_ts = time.time()

			threading.Thread(
				target=submit_measurement,
				args=(value_uSv_h, agg["start_ts"], end_ts, hits_snapshot),
				daemon=True
			).start()
		
		except Exception as e:
			print(f"[API err] {e}")
		agg["start_ts"] = None  # prêt pour la suivante
		agg["hits"] = 0 # prêt pour la suivante


# --- BlueZ helpers ---
def find_char_path(bus, dev_path, uuid):
	mngr = bus.get("org.bluez", "/")
	for path, ifaces in mngr.GetManagedObjects().items():
		gatt = ifaces.get("org.bluez.GattCharacteristic1")
		if gatt and path.startswith(dev_path) and gatt.get("UUID") == uuid:
			return path
	return None

def main():
    bus = SystemBus()
    mngr = bus.get("org.bluez", "/")
    objs = mngr.GetManagedObjects()

    # adaptateur + périphérique
    adapter_path = next(p for p, ifs in objs.items() if "org.bluez.Adapter1" in ifs)
    dev_path = adapter_path + "/dev_" + MAC.replace(":", "_")
    device = bus.get("org.bluez", dev_path)

    # Résolution identité (apparatusId / apparatusVersion) en une fois
    agg["apparatusId"], agg["apparatusVersion"] = resolve_apparatus_identity(bus, adapter_path, device, MAC)
    print(f"[info] apparatusId = {agg['apparatusId']} | apparatusVersion = {agg['apparatusVersion']}")

    # Placeholders pour TX/RX et chemins (serviront dans la reconnexion)
    tx = rx = None
    tx_path = rx_path = None

    # --- Reconnexion simple (backoff 1,2,4,...60s) ---
    backoff_delay = {"d": 1}
    reconnect_timer_id = {"id": None}
    signal_bound = {"done": False}  # <-- NE PAS ré-attacher le handler plusieurs fois

    def on_props_changed(iface, changed, invalidated):
        # Callback notifications (TX)
        if "Value" in changed:
            data = bytes(changed["Value"])
            info = decode_tlv(data)
            ts = time.strftime("%H:%M:%S")
            if info:
                print(f"{ts} {info} ", end="")
            print(f"{ts} raw={data.hex(' ')}")

            # compte instantané
            if "count" in info:
                # 1) forme de trame attendue (comme l'app)
                if not looks_like_step(data):
                    print(f"[STEP] trame ignorée (pas une step valide) raw={data.hex(' ')}")
                    return

                # 2) HV prête ?
                ready, hv, hv_min, ttype = hv_ready_info()
                if not ready:
                    if hv is None or hv_min is None:
                        print("[HV] Attente informations HT/tube…")
                    else:
                        print(f"[HV] Attente HT: {hv:.0f} V < {hv_min:.0f} V (tube {ttype})")
                    return

                # 3) comptage
                if agg["start_ts"] is None:
                    start_measurement()
                agg["hits"] += info["count"]

            # méta & floats utiles
            for k in ("calibCoeff","hv_volts","temperature_c","serial","version","sensorType","tubeType"):
                if k in info:
                    if k == "version":
                        agg["firmwareVersion"] = info[k]
                    elif k == "sensorType":
                        agg["apparatusSensorType"] = (lambda s: ("geiger" if s and "geiger" in s.lower() else ("photodiode" if s and "photo" in s.lower() else None)))(info[k])
                    elif k == "tubeType":
                        agg["apparatusTubeType"] = info[k]
                        # Réglages initiaux à la 1ère annonce du tube
                        set_tube_voltage(rx, info[k])
                        set_visual_hits(rx, True)   # LED ON
                        set_audio_hits(rx, False)   # Buzzer OFF
                    else:
                        agg[k] = info[k]

            maybe_finish_measurement()

    def connect_and_subscribe():
        """(Re)connexion + résolution TX/RX + abonnement notify + GET_INFO."""
        device.Connect()
        # attendre services GATT
        while True:
            try:
                if device.ServicesResolved:
                    break
            except Exception:
                pass
            time.sleep(0.1)

        # caractéristiques TX et RX
        nonlocal tx, rx, tx_path, rx_path
        tx_path = find_char_path(bus, dev_path, UUID_TX)
        rx_path = find_char_path(bus, dev_path, UUID_RX)
        if not tx_path or not rx_path:
            raise RuntimeError("Caractéristique TX/RX introuvable")

        tx = bus.get("org.bluez", tx_path)
        rx = bus.get("org.bluez", rx_path)

        # ATTACHER LE HANDLER UNE SEULE FOIS
        if not signal_bound["done"]:
            tx.onPropertiesChanged = on_props_changed
            signal_bound["done"] = True

        # (ré)demander les notifications et infos
        try:
            tx.StartNotify()
        except Exception:
            # si déjà en cours, ignorer
            pass
        try:
            # Demande d'infos statiques (sensorType / tubeType / etc.)
            rx.WriteValue(bytearray(b"\x12"), {})
        except Exception as e:
            print(f"[warn] Write SEND_INFO: {e}")

        print("[BLE] Connecté et abonné aux notifications.")

    def reconnect_cb():
        """Callback timer GLib pour tenter une reconnexion."""
        reconnect_timer_id["id"] = None
        try:
            connect_and_subscribe()
            backoff_delay["d"] = 1
            print("[BLE] Reconnecté.")
        except Exception as e:
            print(f"[BLE] Reconnexion échouée: {e}")
            schedule_reconnect()
        return False  # ne pas relancer automatiquement

    def schedule_reconnect():
        """Programme une tentative dans d secondes (1,2,4,...,max 60)."""
        if reconnect_timer_id["id"] is not None:
            return
        d = backoff_delay["d"]
        if d > 60:
            d = 60
        print(f"[BLE] Déconnecté — reconnexion dans {d}s…")
        reconnect_timer_id["id"] = GLib.timeout_add_seconds(d, reconnect_cb)
        backoff_delay["d"] = min(d * 2, 60)

    def on_dev_props_changed(iface, changed, invalidated):
        # Surveille la perte de lien BLE et programme une reconnexion
        if "Connected" in changed and not changed["Connected"]:
            schedule_reconnect()

    # Écoute les changements d'état du device (connected/disconnected)
    device.onPropertiesChanged = on_dev_props_changed

    # (Re)connexion initiale + abonnement
    connect_and_subscribe()

    print("Écoute… (Ctrl+C pour quitter)")
    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        try:
            if tx:
                tx.StopNotify()
        except Exception:
            pass
        try:
            device.Disconnect()
        except Exception:
            pass
        # annule un timer de reconnexion en cours
        if reconnect_timer_id["id"] is not None:
            try:
                GLib.source_remove(reconnect_timer_id["id"])
            except Exception:
                pass
            reconnect_timer_id["id"] = None
        print("\nArrêt.")


if __name__ == "__main__":
	main()
