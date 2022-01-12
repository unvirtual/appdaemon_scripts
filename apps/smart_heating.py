from attrs import define, field
from typing import Optional, Union, Any
from itertools import cycle
from collections import deque
import copy
import hassapi as hass
import datetime

DEFAULT_SETMODE = "eco"

def weekday_from_number(n):
    l = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    return l[n-1]

def weekday_str_from_list(l):
    res = []
    for i in l:
        res.append(weekday_from_number(i))
    return ",".join(res)

@define
class ScheduleItem:
    start: datetime.time
    end: datetime.time
    setmode: str
    weekdays: Optional[list[int]] = field(default=None)
    
    @staticmethod
    def _parse_weekdays(s: str):
        tokens = s.split('s')
        res = []
        for t in tokens:
            if '-' in t:
                start, _, end = s.partition('-')
                res.extend(list(range(int(start), int(end) + 1)))
            else:
                res.append(int(t))
        return res

    @classmethod
    def from_dict(cls, dct):
        return cls(
            setmode=dct["setmode"],
            start=datetime.time.fromisoformat(dct["start"]),
            end=datetime.time.fromisoformat(dct["end"]),
            weekdays=cls._parse_weekdays(dct["weekdays"]) if "weekdays" in dct else list(range(1,8))
        )

@define
class Schedule:
    items: list[ScheduleItem]
    name: str

    @classmethod
    def from_list(cls, name, l):
        items = [ScheduleItem.from_dict(d) for d in l]
        items.sort(key=lambda i: i.start)
        return cls(name=name, items=items)

    def get_item_at(self, weekday, time):
        for i in self.items:
            if i.start <= time and i.end > time and weekday in i.weekdays:
                return i
        return None

    def get_item_at_datetime(self, dt):
        return self.get_item_at(dt.isoweekday(), dt.time())

@define
class Thermostat:
    hass: hass
    entity_id: str
    alpha: float = field(default=0.)
    offset: float = field(default=0.)

    MAX_TEMP_SETTING = 28.0

    def get_temperature_setting(self):
        entity = self.hass.get_entity(self.entity_id)
        return entity.get_state(attribute="temperature")

    def get_measured_temperature(self):
        entity = self.hass.get_entity(self.entity_id)
        return entity.get_state(attribute="current_temperature")

    def set_temperature(self, target_temperature, current_temperature, force=False):
        delta = target_temperature - current_temperature
        
        current_measurement = self.get_measured_temperature()
        new_temperature = round((current_measurement + self.alpha*delta + self.offset)*2.)/2.

        new_temperature = max(new_temperature, target_temperature)
        if new_temperature > Thermostat.MAX_TEMP_SETTING:
            new_temperature = Thermostat.MAX_TEMP_SETTING

        current_setting = self.get_temperature_setting()

        if force or new_temperature != current_setting:
            self.hass.log("[Thermostat] {} setting {} -> {} (target: {}, alpha: {}, forced: {})".format(self.entity_id, current_setting, new_temperature, target_temperature, self.alpha, force))
            self.hass.call_service("climate/set_temperature", entity_id=self.entity_id, temperature=new_temperature)
        else:
            self.hass.log("[Thermostat] No setting change (setting: {}, target: {})".format(current_setting, target_temperature))
        self.hass.log("[Thermostat] {}: temp delta (power output) {}".format(self.entity_id, new_temperature - current_measurement))

@define
class Selector:
    entity_id: str
    states: Union[dict[str, Schedule], Schedule]

TEMPERATURE_SENSOR_UPDATED="smart_heating_temperature_sensor_updated"

@define
class TemperatureSensor:
    hass: hass
    entity_id: str
    last_value_time: Optional[datetime.datetime] = field(init=False)
    last_value: Optional[float] = field(init=False)
    threshold: float = field(default=0.0)

    def __attrs_post_init__(self):
        entity = self.hass.get_entity(self.entity_id)
        if entity is None:
            raise ValueError("Temperature sensor " + entity_id + " not found in HASS")
        self.last_value = self.measure_temperature()
        self.last_value_time = self.hass.get_now()
        entity.listen_state(self.on_change)

    def measure_temperature(self):
        return self._valid_temperature_or_none(self.hass.get_state(self.entity_id))

    def last_temperature(self):
        return self.last_value
        
    def last_temperature_time(self):
        return self.last_value_time

    def _valid_temperature_or_none(self, value):
        try:
            return float(value) 
        except ValueError:
            return None

    def on_change(self, entity, attribute, old, new, kwargs):
        self.hass.log("[TempSensor] {} temperature {} -> {}".format(self.entity_id, old, new))
        new_temp = self._valid_temperature_or_none(new)
        changed_from_to_none = new_temp != self.last_value and (new_temp is None or self.last_value is None) 
        if changed_from_to_none or abs(new_temp - self.last_value) > self.threshold:
            self.last_value = new_temp
            self.last_value_time = self.hass.get_now()
            self.hass.fire_event(TEMPERATURE_SENSOR_UPDATED, entity_id=self.entity_id, temperature=self.last_value)

    @classmethod
    def create(cls, hass, entity_id):
        return cls(hass=hass, entity_id=entity_id)

@define
class Room:
    hass: hass
    name: str
    thermostats: list[Thermostat]
    modes: dict[str, float]    
    default_schedule: Schedule
    temperature_sensors: list[TemperatureSensor]
    handles: list[Any] = field(default=[])
    overrides: list[str] = field(default=[])
    conditionals: list[Any] = field(default=[])
    default_mode: str = field(default="eco")
    _current_schedule: Schedule = field(init=False)

    def __attrs_post_init__(self):
        self._current_schedule = self.default_schedule
        self.hass.listen_event(self.on_temperature_changed, TEMPERATURE_SENSOR_UPDATED)
        for t in self.thermostats:
            entity = self.hass.get_entity(t.entity_id)
            entity.listen_state(self.on_preset_mode_change, attribute="preset_mode")

        for i in self.conditionals:
            entity_id = i.get("entity_id")
            entity = self.hass.get_entity(entity_id)
            entity.listen_state(self.on_conditional_changed)

        self.update_schedule(force=True)
        self.update_thermostats(force=True)

        self.hass.log("==================== ")
        self.hass.log("Room {} initialized:".format(self.name))
        self.hass.log("  current room temperature: {}".format(self.get_room_temperature()))
        self.hass.log("  current schedule:         {}".format(self._current_schedule.name))
        self.hass.log("  current mode:             {}".format(self.get_current_mode()))
        self.hass.log("  modes:                    {}".format(self.modes))
        self.hass.log("==================== ")

    def on_preset_mode_change(self, entity, attribute, old, new, kwargs):
        if entity not in [x.entity_id for x in self.thermostats]:
            return
        if new is not None:
            self.hass.log("room {} resetting mode to manual for thermostat {}".format(self.name, entity))
            self.update_thermostats(force=True)

    def get_room_temperature(self):
        temps = list(filter(lambda x: x is not None, [y.last_temperature() for y in self.temperature_sensors]))
        if len(temps) == 0:
            self.hass.log(
                "{}: all (n={}) temperature sensors return no values. Using thermostat temperature.".format(self.name, len(self.temperature_sensors)), 
                level="WARNING"
            )
            return self.hass.get_state(self.thermostats[0].entity_id, attribute="current_temperature")
        elif len(temps) < len(self.temperature_sensors):
            self.hass.log(
                "{}: some temperature sensors return no values. Using arbitrary temperature sensor.".format(self.name), 
                level="WARNING"
            )
            return temps[0]
        else:
            return sum(temps)/len(temps)

    def on_temperature_changed(self, event_name, data, kwargs):
        if data["entity_id"] in [x.entity_id for x in self.temperature_sensors]:
            self.update_thermostats()

    def on_conditional_changed(self, entity, attribute, old, new, kwargs):
        self.hass.log("Room {}: condition {} changed from {} to {}".format(self.name, entity, old, new))
        if new == old:
            return
        updated = self.update_schedule()
        if updated:
            self.hass.log("Room {}: condition change triggered new schedule {}".format(self.name, self._current_schedule.name))
            self.update_thermostats(add_offset_seconds=10)
        else:
            self.hass.log("Room {}: condition change without schedule change".format(self.name))

    def get_conditional_state(self):
        res = []
        for c in self.conditionals:
            state = self.hass.get_state(c["entity_id"])
            if state in c["values"]:
                res.append({"entity_id": c["entity_id"], "state": state, "schedule": c["values"][state]})
        return res

    def conditional_schedule(self):
        schedule = None
        c_states = self.get_conditional_state()
        # assume only one element for now
        if len(c_states) > 0:
            schedule = c_states[0]["schedule"]
        return schedule

    def update_schedule(self, force=False):
        c_schedule = self.conditional_schedule() 
        c_schedule = c_schedule if c_schedule is not None else self.default_schedule
        if c_schedule != self._current_schedule or force:
            self._current_schedule = c_schedule
            self.cancel_scheduled_events()
            self.schedule_events()
            self.publish_target_temperature()
            return True
        return False

    def get_current_mode(self, add_offset_seconds=0):
        scheduled = self._current_schedule.get_item_at_datetime(
            self.hass.get_now() + datetime.timedelta(seconds=add_offset_seconds)
        )
        return scheduled.setmode if scheduled is not None else DEFAULT_SETMODE

    def update_thermostats(self, add_offset_seconds=0, force=False):
        mode = self.get_current_mode(add_offset_seconds)
        for t in self.thermostats:
            t.set_temperature(self.modes[mode], self.get_room_temperature(), force=force)

    def publish_target_temperature(self, kwargs=None):
        mode = self.get_current_mode()
        self.hass.log("Room {} target temp published: {}".format(self.name, self.modes[mode]))
        self.hass.set_state("sensor.target_temperature_" + self.name, state=self.modes[mode])

    def cancel_scheduled_events(self):
        self.hass.log("Cancelling schedule for room {}".format(self.name), level="DEBUG")
        for handle in self.handles:
            self.hass.cancel_timer(handle)
        self.handles = []

    def schedule_events(self):
        self.hass.log("Room {}: scheduling events for schedule {}".format(self.name, self._current_schedule.name), level="DEBUG")
        for item in self._current_schedule.items:
            self.handles.append(
                self.hass.run_daily(
                    self.set_mode_callback, 
                    item.start, 
                    setmode=item.setmode, 
                    constrain_days=weekday_str_from_list(item.weekdays)
                )
            )
            self.handles.append(
                self.hass.run_daily(
                    self.set_mode_callback, 
                    item.end, 
                    setmode=DEFAULT_SETMODE,
                    constrain_days=weekday_str_from_list(item.weekdays)
                )
            )

    def set_mode_callback(self, kwargs):
        self.hass.log("Room {}: mode changed to {}".format(str(self.name), kwargs["setmode"]))
        self.publish_target_temperature()
        self.update_thermostats(add_offset_seconds=10)

    @classmethod
    def replace_conditional_schedules(cls, conditionals, schedules):
        res = copy.deepcopy(conditionals)
        for i in res:
            for k,v in i["values"].items():
                i["values"][k] = schedules[v]
        return res

    @classmethod
    def merge_modes(cls, default_modes, custom_modes):
        modes = {}
        for k,v in default_modes.items():
            modes[k] = custom_modes[k] if k in custom_modes else v
        return modes

    @classmethod
    def from_dict(cls, hass, name, dct, default_modes, schedules):
        conditionals = dct.get("conditional_schedules") or []
        conditionals = cls.replace_conditional_schedules(conditionals, schedules)
        custom_modes = cls.merge_modes(default_modes, dct.get("modes") or {})

        return cls(
            hass=hass,
            name=name,
            thermostats=[Thermostat(hass=hass, **e) for e in dct["thermostats"]],
            default_schedule=schedules[dct["default_schedule"]],
            modes=custom_modes,
            temperature_sensors=[TemperatureSensor.create(hass, e) for e in dct["temperature_sensors"]] ,
            conditionals=conditionals
        )


class SmartHeating(hass.Hass):
    def initialize(self):
        self.log("SmartHeating started")
        self.schedules = {}
        self.rooms = {}
        self.default_modes = self.args["default_modes"]

        for k,v in self.args["schedules"].items():
            s = Schedule.from_list(k, v)
            self.schedules[s.name] = s
        for k,v in self.args["rooms"].items():
            r = Room.from_dict(self, k, v, self.default_modes, self.schedules)
            self.rooms[r.name] = r