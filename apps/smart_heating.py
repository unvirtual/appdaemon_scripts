from attrs import define, field
from typing import Optional, Union, Any
from itertools import cycle
from collections import deque, OrderedDict
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

    def get_next_item_at(self, weekday, time, remaining_weekdays=7):
        # assumes a sorted schedule with no overlaps and no items schedule over midnight!
        l = []
        for i in self.items:
            if weekday in i.weekdays:
                l.append(i)

        for i in l:
            if time < i.start:
                return ("next", i, 7 - remaining_weekdays)
            if time >= i.start and time < i.end:
                return ("current", i, 7 - remaining_weekdays)

        if remaining_weekdays == 0:
            return None

        res = self.get_next_item_at(weekday % 7 + 1, datetime.time(0,0,1), remaining_weekdays=remaining_weekdays-1)
        if res is not None:
            return res

        return None

    def get_item_at_datetime(self, dt):
        return self.get_item_at(dt.isoweekday(), dt.time())

    def get_next_item_at_datetime(self, dt):
        n = self.get_next_item_at(dt.isoweekday(), dt.time())
        date = datetime.date.today() + datetime.timedelta(days=n[2])
        return (n, date)


@define
class Thermostat:
    hass: hass
    entity_id: str
    alpha: float = field(default=0.)
    offset: float = field(default=0.)

    MAX_TEMP_SETTING = 28.0
    MIN_TEMP_SETTING = 17.0

    def get_temperature_setting(self):
        entity = self.hass.get_entity(self.entity_id)
        return entity.get_state(attribute="temperature")

    def get_measured_temperature(self):
        entity = self.hass.get_entity(self.entity_id)
        return entity.get_state(attribute="current_temperature")

    def set_temperature(self, target_temperature, current_temperature, force=False):
        delta = target_temperature - current_temperature
        new_temperature = target_temperature

        current_measurement = self.get_measured_temperature()
        if delta > 0:
            new_temperature = round((current_measurement + self.alpha*delta + self.offset)*2.)/2.
            new_temperature = min(max(new_temperature, target_temperature), Thermostat.MAX_TEMP_SETTING)
        else:
            new_temperature = Thermostat.MIN_TEMP_SETTING

        current_setting = self.get_temperature_setting()

        if force or new_temperature != current_setting:
            self.hass.log("[Thermostat] {} setting {} -> {} (target: {}, alpha: {}, forced: {})".format(self.entity_id, current_setting, new_temperature, target_temperature, self.alpha, force), level="DEBUG")
            self.hass.call_service("climate/set_temperature", entity_id=self.entity_id, temperature=new_temperature)
        else:
            self.hass.log("[Thermostat] {}: No setting change (setting: {}, target: {})".format(self.entity_id, current_setting, target_temperature), level="DEBUG")
        self.hass.log("[Thermostat] {}: temp delta (power output) {} (room temp: {}, new temp: {}, thermostat temp: {})".format(self.entity_id, new_temperature - current_measurement, current_temperature, new_temperature, current_measurement), level="DEBUG")

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
        self.hass.log("[TempSensor] {} temperature {} -> {}".format(self.entity_id, old, new), level="DEBUG")
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
class RoomThermostat:
    hass: hass
    name: str
    entity: str
    auto_target_temp: float
    manual: bool
    thermostats: list[Thermostat]
    temperature_sensors: list[TemperatureSensor]

    def __attrs_post_init__(self):
        self.hass.listen_event(self._on_sensor_temperature_changed, TEMPERATURE_SENSOR_UPDATED)
        for t in self.thermostats:
            entity = self.hass.get_entity(t.entity_id)
            entity.listen_state(self._on_thermostat_temperature_changed, attribute="current_temperature")

        entity = self.hass.get_entity(self.entity)
        entity.listen_state(self._on_target_temperature_changed, attribute="temperature")
        entity.listen_state(self._on_turn_off)
        self._publish_auto_state()

    def is_manual():
        return self.manual

    def get_auto_target_temperature(self):
        return self.auto_target_temp

    def set_auto_target_temperature(self, value):
        self.auto_target_temp = value
        self.hass.log("{} setting auto target temp to {}".format(self.name, self.auto_target_temp), level="DEBUG")

    def get_target_temperature(self):
        return self.hass.get_state(self.entity, attribute="temperature")

    def reset_to_auto(self):
        self.hass.log("{} resetting to auto".format(self.name), level="DEBUG")
        if self.auto_target_temp is not None:
            self._set_target_temperature(self.auto_target_temp)

    def measure_temperature(self):
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

    def _set_target_temperature(self, value):
        self.hass.call_service("climate/set_temperature", entity_id=self.entity, temperature=value)

    def _update_thermostats(self, add_offset_seconds=0, force=False):
        room_temp = self.measure_temperature()
        target_temp = self.get_target_temperature()
        for t in self.thermostats:
            t.set_temperature(target_temp, room_temp, force=force)

    def _on_sensor_temperature_changed(self, event_name, data, kwargs):
        if data["entity_id"] in [x.entity_id for x in self.temperature_sensors]:
            self.hass.log("Room {} on_sensor_temperature_changed() updates thermostats", level="DEBUG")
            self._update_thermostats()

    def _on_thermostat_temperature_changed(self, entity, attribute, old, new, kwargs):
        if entity in [x.entity_id for x in self.thermostats]:
            self.hass.log("Room {} on_thermostat_temperature_changed() updates thermostats", level="DEBUG")
            self._update_thermostats()

    def _on_target_temperature_changed(self, entity, attribute, old, new, kwargs):
        self.hass.log("{} _on_target_temperature_changed. new: {}, old: {}".format(self.name, new, old))
        if new == old:
            return
        self.manual = new != self.auto_target_temp
        self.hass.log("{} manual mode: {}".format(self.name, self.manual))
        self._update_thermostats()
        self._publish_auto_state()

    def _on_turn_off(self, entity, attribute, old, new, kwargs):
        self.hass.log("{} _on_turn_off".format(self.name), level="DEBUG")
        if new == old:
            return
        if new == "off":
            self.reset_to_auto()
            self.hass.call_service("climate/turn_on", entity_id=self.entity)

    def _publish_auto_state(self):
        self.hass.set_state("sensor.{}_manual_mode".format(self.name), state=self.manual)

    @classmethod
    def from_dict(cls, hass, dct, name, auto_target_temp, manual):
        return cls(
            hass=hass,
            name="room_thermostat_{}".format(name),
            entity=dct["control"],
            auto_target_temp=auto_target_temp,
            manual=manual,
            thermostats=[Thermostat(hass=hass, **e) for e in dct["thermostats"]],
            temperature_sensors=[TemperatureSensor.create(hass, e) for e in dct["temperature_sensors"]]
        )

@define
class Room:
    hass: hass
    name: str
    room_thermostat: RoomThermostat
    modes: dict[str, float]    
    default_schedule: Schedule
    handles: list[Any] = field(default=[])
    conditionals: list[Any] = field(default=[])
    default_mode: str = field(default="eco")
    _current_schedule: Schedule = field(init=False)

    def __attrs_post_init__(self):
        self._current_schedule = self.default_schedule

        self._update_schedule(force=True)

        self.hass.log("==================== ")
        self.hass.log("Room {} initialized:".format(self.name))
        self.hass.log("  current room temperature: {}".format(self.get_room_temperature()))
        self.hass.log("  current schedule:         {}".format(self._current_schedule.name))
        self.hass.log("  current mode:             {}".format(self.current_state()))
        self.hass.log("  modes:                    {}".format(self.modes))
        self.hass.log("  next:                     {}".format(self.get_next_state()))
        self.hass.log("==================== ")
        self.update_ha_sensor_state()

    def update_ha_sensor_state(self):
        res = self.get_next_state()
        self.hass.set_state(
            "sensor.room_thermostat_{}_next_temperature".format(self.name), 
            state=res["next-target-temperature"], 
            next_time=res["next-time"],
            next_mode=res["next-mode"]
        )

    def get_room_temperature(self):
        return self.room_thermostat.measure_temperature()

    def current_state(self, add_offset_seconds=0):
        scheduled = self._current_schedule.get_item_at_datetime(
            self.hass.get_now() + datetime.timedelta(seconds=add_offset_seconds)
        )
        mode = scheduled.setmode if scheduled is not None else DEFAULT_SETMODE
        temp = self.modes[mode]
        return {"mode": mode, "target-temperature": temp}

    def get_next_state(self):
        next_item, date = self._current_schedule.get_next_item_at_datetime(self.hass.get_now())

        # mode
        if next_item is None or next_item[0] == "current":
            mode = DEFAULT_SETMODE
        else:
            mode = next_item[1].setmode

        # temperature
        temp = self.modes[mode]

        # next time switch
        if next_item is None:
            time = None
        else:
            if next_item[0] == "current":
                time = next_item[1].end
            else:
                time = next_item[1].start

        return {"next-time": datetime.datetime.combine(date,time), "next-mode": mode, "next-target-temperature": temp}

    def set_target_temperature_from_schedule(self, add_offset_seconds=0, kwargs=None):
        sched_temperature = self.current_state(add_offset_seconds=add_offset_seconds)["target-temperature"]
        self.hass.log("Room {} target temp set: {}".format(self.name, sched_temperature))
        self.room_thermostat.set_auto_target_temperature(sched_temperature)
        self.room_thermostat.reset_to_auto()
        self.update_ha_sensor_state()

    def conditional_has_changed(self, entity, attribute, old, new, kwargs):
        if entity not in self.conditionals and new == old:
            return
        self._update_schedule()

    def _update_schedule(self, force=False):
        c_schedule = None
        # determine if default or conditional schedule should be used
        c_states = []
        for c in self.conditionals:
            state = self.hass.get_state(c["entity_id"])
            if state in c["values"]:
                c_states.append({"entity_id": c["entity_id"], "state": state, "schedule": c["values"][state]})

        # assume only one element for now
        if len(c_states) > 0:
            c_schedule = c_states[0]["schedule"]

        c_schedule = c_schedule if c_schedule is not None else self.default_schedule
        if c_schedule != self._current_schedule or force:
            self._current_schedule = c_schedule
            self._cancel_scheduled_events()
            self._schedule_events()
            self.set_target_temperature_from_schedule()
            return True
        return False

    def _cancel_scheduled_events(self):
        self.hass.log("Cancelling schedule for room {}".format(self.name), level="DEBUG")
        for handle in self.handles:
            self.hass.cancel_timer(handle)
        self.handles = []

    def _schedule_events(self):
        self.hass.log("Room {}: scheduling events for schedule {}".format(self.name, self._current_schedule.name), level="DEBUG")
        for item in self._current_schedule.items:
            self.handles.append(
                self.hass.run_daily(
                    self._set_mode_callback, 
                    item.start, 
                    setmode=item.setmode, 
                    constrain_days=weekday_str_from_list(item.weekdays)
                )
            )
            self.handles.append(
                self.hass.run_daily(
                    self._set_mode_callback, 
                    item.end, 
                    setmode=DEFAULT_SETMODE,
                    constrain_days=weekday_str_from_list(item.weekdays)
                )
            )

    def _set_mode_callback(self, kwargs):
        self.hass.log("Room {}: mode changed to {}".format(str(self.name), kwargs["setmode"]))
        self.set_target_temperature_from_schedule(add_offset_seconds=10)

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
        auto_target_temp = custom_modes[DEFAULT_SETMODE]

        return cls(
            hass=hass,
            name=name,
            room_thermostat=RoomThermostat.from_dict(hass, dct, name=name, auto_target_temp=auto_target_temp, manual=False),
            default_schedule=schedules[dct["default_schedule"]],
            modes=custom_modes,
            conditionals=conditionals
        )

class SmartHeating(hass.Hass):
    def initialize(self):
        self.log("SmartHeating started")
        self.schedules = {}
        self.rooms = {}
        self.default_modes = self.args["default_modes"]
        self.reset_handle = None

        conditionals = []
        for k,v in self.args["schedules"].items():
            s = Schedule.from_list(k, v)
            self.schedules[s.name] = s
        for k,v in self.args["rooms"].items():
            r = Room.from_dict(self, k, v, self.default_modes, self.schedules)
            self.rooms[r.name] = r
            conditionals.extend([x["entity_id"] for x in r.conditionals])

        for i in set(conditionals):
            self.log("App subscribing to {}".format(i))
            entity = self.get_entity(i)
            entity.listen_state(self.on_conditional_changed)

    def on_conditional_changed(self, entity, attribute, old, new, kwargs):
        self.log("condition {} changed from {} to {}".format(entity, old, new))
        for r in self.rooms.values():
            r.conditional_has_changed(entity, attribute, old, new, kwargs)