#!/usr/bin/env python3
import datetime
import os
import signal
import subprocess
import sys
import traceback
from typing import List, Tuple, Union

from cereal import log
import cereal.messaging as messaging
import openpilot.selfdrive.sentry as sentry
from openpilot.common.basedir import BASEDIR
from openpilot.common.params import Params, ParamKeyType
from openpilot.common.text_window import TextWindow
from openpilot.selfdrive.boardd.set_time import set_time
from openpilot.system.hardware import HARDWARE, PC
from openpilot.selfdrive.manager.helpers import unblock_stdout, write_onroad_params
from openpilot.selfdrive.manager.process import ensure_running
from openpilot.selfdrive.manager.process_config import managed_processes
from openpilot.selfdrive.athena.registration import register, UNREGISTERED_DONGLE_ID, is_registered_device
from openpilot.system.swaglog import cloudlog, add_file_handler
from openpilot.system.version import is_dirty, get_commit, get_version, get_origin, get_short_branch, \
                           get_normalized_origin, terms_version, training_version, \
                           is_tested_branch, is_release_branch

import json
# for mapd
from custom_dep import THIRD_PARTY_DIR
sys.path.append(THIRD_PARTY_DIR)

def manager_init() -> None:
  # update system time from panda
  set_time(cloudlog)

  # save boot log
  subprocess.call("./bootlog", cwd=os.path.join(BASEDIR, "system/loggerd"))

  params = Params()
  params.clear_all(ParamKeyType.CLEAR_ON_MANAGER_START)
  params.clear_all(ParamKeyType.CLEAR_ON_ONROAD_TRANSITION)
  params.clear_all(ParamKeyType.CLEAR_ON_OFFROAD_TRANSITION)

  default_params: List[Tuple[str, Union[str, bytes]]] = [
    ("CompletedTrainingVersion", "0"),
    ("DisengageOnAccelerator", "0"),
    ("GsmMetered", "1"),
    ("HasAcceptedTerms", "0"),
    ("LanguageSetting", "main_en"),
    ("OpenpilotEnabledToggle", "1"),
    ("LongitudinalPersonality", str(log.LongitudinalPersonality.standard)),

    # dp
    ("dp_alka", "0"),
    ("dp_device_ip_addr", ""),
    ("dp_device_auto_shutdown", "0"),
    ("dp_device_auto_shutdown_in", "30"),
    ("dp_device_disable_temp_check", "0"),
    ("dp_device_display_off_mode", "0"),
    ("dp_device_audible_alert_mode", "0"),
    ("dp_toyota_auto_lock", "0"),
    ("dp_toyota_auto_unlock", "0"),
    ("dp_toyota_sng", "0"),
    ("dp_toyota_enhanced_bsm", "0"),
    ("dp_toyota_enhanced_long_tune", "0"),
    ("dp_mapd", "0"),
    ("dp_mapd_vision_turn_control", "0"),
    ("dp_mapd_speed_limit_control", "0"),
    ("dp_mapd_turn_speed_control", "0"),
    ("dp_fileserv", "0"),
    ("dp_otisserv", "0"),
    ("dp_long_accel_profile", "0"),
    ("dp_long_use_df_tune", "0"),
    ("dp_long_de2e", "0"),
    ("dp_device_dm_unavailable", "0"),
    ("dp_long_taco", "0"),
    ("dp_long_stock_mode", "0"),
    ("dp_long_missing_lead_warning", "0"),
    ("dp_lateral_road_edge_detection", "0"),
    ("dp_nav_voice_guidance", "0"),
    ("dp_long_frogai_aggre_accel_tune", "0"),
    ("dp_toyota_zss", "0"),
    ("dp_long_accel_btn", "0"),
    ("dp_long_personality_btn", "0"),
    ("dp_disable_onroad_uploads", "0"),
    ("dp_long_frogai_smooth_braking_tune", "0"),
  ]
  if not PC:
    default_params.append(("LastUpdateTime", datetime.datetime.utcnow().isoformat().encode('utf8')))

  params.put("dp_car_list", get_support_car_list())

  if params.get_bool("RecordFrontLock"):
    params.put_bool("RecordFront", True)

  # set unset params
  for k, v in default_params:
    if params.get(k) is None:
      params.put(k, v)

  # is this dashcam?
  if os.getenv("PASSIVE") is not None:
    params.put_bool("Passive", bool(int(os.getenv("PASSIVE", "0"))))

  if params.get("Passive") is None:
    raise Exception("Passive must be set to continue")

  # Create folders needed for msgq
  try:
    os.mkdir("/dev/shm")
  except FileExistsError:
    pass
  except PermissionError:
    print("WARNING: failed to make /dev/shm")

  # set version params
  params.put("Version", get_version())
  params.put("TermsVersion", terms_version)
  params.put("TrainingVersion", training_version)
  params.put("GitCommit", get_commit(default=""))
  params.put("GitBranch", get_short_branch(default=""))
  params.put("GitRemote", get_origin(default=""))
  params.put_bool("IsTestedBranch", is_tested_branch())
  params.put_bool("IsReleaseBranch", is_release_branch())

  # set dongle id
  reg_res = register(show_spinner=True)
  if reg_res:
    dongle_id = reg_res
  else:
    serial = params.get("HardwareSerial")
    raise Exception(f"Registration failed for device {serial}")
  os.environ['DONGLE_ID'] = dongle_id  # Needed for swaglog

  if not is_dirty():
    os.environ['CLEAN'] = '1'

  # init logging
  sentry.init(sentry.SentryProject.SELFDRIVE)
  cloudlog.bind_global(dongle_id=dongle_id,
                       version=get_version(),
                       origin=get_normalized_origin(),
                       branch=get_short_branch(),
                       commit=get_commit(),
                       dirty=is_dirty(),
                       device=HARDWARE.get_device_type())


def manager_prepare() -> None:
  for p in managed_processes.values():
    p.prepare()


def manager_cleanup() -> None:
  # send signals to kill all procs
  for p in managed_processes.values():
    p.stop(block=False)

  # ensure all are killed
  for p in managed_processes.values():
    p.stop(block=True)

  cloudlog.info("everything is dead")


def manager_thread() -> None:
  cloudlog.bind(daemon="manager")
  cloudlog.info("manager start")
  cloudlog.info({"environ": os.environ})

  params = Params()

  ignore: List[str] = []
  if params.get("DongleId", encoding='utf8') in (None, UNREGISTERED_DONGLE_ID):
    ignore += ["manage_athenad", "uploader"]
  if os.getenv("NOBOARD") is not None:
    ignore.append("pandad")
  ignore += [x for x in os.getenv("BLOCK", "").split(",") if len(x) > 0]

  # dp
  if not params.get_bool("dp_mapd"):
    ignore += ["mapd"]

  if not params.get_bool("dp_fileserv"):
    ignore += ["fileserv"]

  if not params.get_bool("dp_otisserv"):
    ignore += ["otisserv"]

  dpdmonitoringd_ignored = True
  if not is_registered_device():
    ignore += ["uploader"]
    if params.get_bool("dp_device_dm_unavailable"):
      ignore += ["dmonitoringd", "dmonitoringmodeld"]
      dpdmonitoringd_ignored = False
  if dpdmonitoringd_ignored:
    ignore += ["dpdmonitoringd"]

  sm = messaging.SubMaster(['deviceState', 'carParams'], poll=['deviceState'])
  pm = messaging.PubMaster(['managerState'])

  write_onroad_params(False, params)
  ensure_running(managed_processes.values(), False, params=params, CP=sm['carParams'], not_run=ignore)

  started_prev = False

  while True:
    sm.update()

    started = sm['deviceState'].started

    if started and not started_prev:
      params.clear_all(ParamKeyType.CLEAR_ON_ONROAD_TRANSITION)
    elif not started and started_prev:
      params.clear_all(ParamKeyType.CLEAR_ON_OFFROAD_TRANSITION)

    # update onroad params, which drives boardd's safety setter thread
    if started != started_prev:
      write_onroad_params(started, params)

    started_prev = started

    ensure_running(managed_processes.values(), started, params=params, CP=sm['carParams'], not_run=ignore)

    running = ' '.join("%s%s\u001b[0m" % ("\u001b[32m" if p.proc.is_alive() else "\u001b[31m", p.name)
                       for p in managed_processes.values() if p.proc)
    print(running)
    cloudlog.debug(running)

    # send managerState
    msg = messaging.new_message('managerState')
    msg.managerState.processes = [p.get_process_state_msg() for p in managed_processes.values()]
    pm.send('managerState', msg)

    # Exit main loop when uninstall/shutdown/reboot is needed
    shutdown = False
    for param in ("DoUninstall", "DoShutdown", "DoReboot"):
      if params.get_bool(param):
        shutdown = True
        params.put("LastManagerExitReason", f"{param} {datetime.datetime.now()}")
        cloudlog.warning(f"Shutting down manager - {param} set")

    if shutdown:
      break


def main() -> None:
  prepare_only = os.getenv("PREPAREONLY") is not None

  manager_init()

  # Start UI early so prepare can happen in the background
  if not prepare_only:
    managed_processes['ui'].start()

  manager_prepare()

  if prepare_only:
    return

  # SystemExit on sigterm
  signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(1))

  try:
    manager_thread()
  except Exception:
    traceback.print_exc()
    sentry.capture_exception()
  finally:
    manager_cleanup()

  params = Params()
  if params.get_bool("DoUninstall"):
    cloudlog.warning("uninstalling")
    HARDWARE.uninstall()
  elif params.get_bool("DoReboot"):
    cloudlog.warning("reboot")
    HARDWARE.reboot()
  elif params.get_bool("DoShutdown"):
    cloudlog.warning("shutdown")
    HARDWARE.shutdown()

def get_support_car_list():
  attrs = ['FINGERPRINTS', 'FW_VERSIONS']
  cars = dict({"cars": []})
  models = []
  for car_folder in [x[0] for x in os.walk('/data/openpilot/selfdrive/car')]:
    try:
      car_name = car_folder.split('/')[-1]
      if car_name not in ("mock", "body", "torque_data", "tests"):
        for attr in attrs:
          values = __import__('selfdrive.car.%s.values' % car_name, fromlist=[attr])
          if hasattr(values, attr):
            attr_values = getattr(values, attr)
          else:
            continue
          if isinstance(attr_values, dict):
            for f, v in attr_values.items():
              if f not in models:
                models.append(f)
    except (ImportError, IOError, ValueError):
      pass
  models.sort()
  cars["cars"] = models
  return json.dumps(cars)

if __name__ == "__main__":
  unblock_stdout()

  try:
    main()
  except Exception:
    add_file_handler(cloudlog)
    cloudlog.exception("Manager failed to start")

    try:
      managed_processes['ui'].stop()
    except Exception:
      pass

    # Show last 3 lines of traceback
    error = traceback.format_exc(-3)
    error = "Manager failed to start\n\n" + error
    with TextWindow(error) as t:
      t.wait_for_exit()

    raise

  # manual exit because we are forked
  sys.exit(0)
