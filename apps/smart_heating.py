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
    entity_id: str

    def set_mode(self, hass, mode):
        hass.log(">>>> Thermostat: Setting mode of " + self.entity_id + " to: " + mode)
        hass.call_service("climate/set_preset_mode", entity_id=self.entity_id, preset_mode=mode)
        
@define
class Selector:
    entity_id: str
    states: Union[dict[str, Schedule], Schedule]

ROOM_UPDATED="on_manual_change"

@define
class Room:
    hass: hass
    name: str
    thermostats: list[str]
    default_schedule: Schedule
    overrides: list[str] = field(default=[])
    conditionals: list[Any] = field(default=[])
    default_mode: str = field(default="eco")
    _current_schedule: Schedule = field(init=False)

    def __attrs_post_init__(self):
        self._current_schedule = self.default_schedule
        self.set_all_conditionals_from_hass()
        for i in self.conditionals:
            entity_id = i.get("entity_id")
            entity = self.hass.get_entity(entity_id)
            entity.listen_state(self.on_conditional_changed)

    def conditional_schedule(self, entity_id, value):
        for c in self.conditionals:
            if c["entity_id"] == entity_id and value in c["values"]:
                return c["values"][value]
        return None

    def set_all_conditionals_from_hass(self):
        for c in self.conditionals:
            state = self.hass.get_state(c["entity_id"])
            self.set_conditional(c["entity_id"], state)

    def set_conditional(self, entity_id, value):
        c_sched = self.conditional_schedule(entity_id, value) or self.default_schedule
        if c_sched != self._current_schedule:
            self._current_schedule = c_sched
            self.hass.fire_event(ROOM_UPDATED, room=self.name)

    def on_conditional_changed(self, entity, attribute, old, new, kwargs):
        if new == old:
            return
        self.set_conditional(entity, new)

    @classmethod
    def replace_conditional_schedules(cls, conditionals, schedules):
        res = copy.deepcopy(conditionals)
        for i in res:
            for k,v in i["values"].items():
                i["values"][k] = schedules[v]
        return res

    @classmethod
    def from_dict(cls, hass, name, dct, schedules):
        conditionals = dct.get("conditional_schedules") or []
        conditionals = cls.replace_conditional_schedules(conditionals, schedules)

        return cls(
            hass=hass,
            name=name,
            thermostats=[Thermostat(entity_id=eid) for eid in dct["thermostats"]],
            default_schedule=schedules[dct["default_schedule"]],
            conditionals=conditionals
        )

    def set_mode(self, mode):
        self.hass.log(">> Room: Setting mode in " + self.name + " to " + mode)
        for t in self.thermostats:
            t.set_mode(self.hass, mode)

    def current_schedule(self):
        return self._current_schedule

    def set_currently_scheduled_mode(self, add_offset_seconds=0):
        scheduled = self._current_schedule.get_item_at_datetime(
            self.hass.get_now() + datetime.timedelta(seconds=add_offset_seconds)
        )
        mode = scheduled.setmode if scheduled is not None else DEFAULT_SETMODE
        self.set_mode(mode)

class SmartHeating(hass.Hass):
    def initialize(self):
        self.log("SmartHeating started")
        self.schedules = {}
        self.rooms = {}
        self.handles = {}

        self.listen_event(self.room_updated, ROOM_UPDATED)

        for k,v in self.args["schedules"].items():
            s = Schedule.from_list(k, v)
            self.schedules[s.name] = s
        for k,v in self.args["rooms"].items():
            r = Room.from_dict(self, k, v, self.schedules)
            self.rooms[r.name] = r
            r.set_currently_scheduled_mode()
        for k,v in self.rooms.items():
            self.set_room_schedule(v)

    def room_updated(self, event_name, data, kwargs):
        room = self.rooms[data["room"]]
        self.log("App received update for room " + room.name)
        self.cancel_room_schedule(room.name)
        room.set_currently_scheduled_mode()
        self.set_room_schedule(room)
        self.log("Currently selected schedules: ")
        for k,v in self.rooms.items():
            self.log(k + ": " + v._current_schedule.name)

    def cancel_room_schedule(self, room_name):
        self.log("Cancelling schedule for room " + room_name)
        if room_name not in self.handles:
            self.log("No schedule found")
            return
        for handle in self.handles[room_name]:
            self.cancel_timer(handle)
        self.handles[room_name] = []

    def cancel_schedules(self):
        for room_name in self.rooms.keys():
            self.cancel_room_schedule(room_name)

    def print_scheduled_events(self, room_name):
        self.log("Timers set for room: " + room_name)
        for i in self.handles[room_name]:
            res = self.info_timer(i)
            if res is not None:
                self.log(str(res))
            else:
                self.log("No timers set")

    def set_room_schedule(self, room):
        sched = room.current_schedule()
        self.log("Setting schedule " + sched.name + " for room " + room.name)
        self.handles[room.name] = []

        for item in sched.items:
            self.handles[room.name].append(
                self.run_daily(self.set_mode_callback, 
                               item.start, 
                               setmode=item.setmode, 
                               room=room.name,
                               constrain_days=weekday_str_from_list(item.weekdays)
                )
            )
            self.handles[room.name].append(
                self.run_daily(self.set_mode_callback, 
                               item.end, 
                               setmode=DEFAULT_SETMODE,
                               room=room.name,
                               constrain_days=weekday_str_from_list(item.weekdays)
                )
            )

    def set_mode_callback(self, kwargs):
        self.log("Callback: " + str(self.get_now()) + ": Setting mode for " + kwargs["room"] + " to " + kwargs["setmode"])
        self.rooms[kwargs["room"]].set_currently_scheduled_mode(add_offset_seconds=10)



