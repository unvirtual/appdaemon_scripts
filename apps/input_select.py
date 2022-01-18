import hassapi as hass
import datetime

class InputSelect(hass.Hass):
    def initialize(self):
        self.entity_id = self.args["entity_id"]
        self.default_value = self.args["default_value"]
        self.default_reset_time = self.args["default_reset_time"]

        self.handle = None
        self.listen_state(self.on_state_changed, self.entity_id)
        self.publish_next_change(None, None)
        self.schedule_reset()

    def on_state_changed(self, *args, **kwargs):
        self.schedule_reset()

    def schedule_reset(self):
        state = self.get_state(self.entity_id)
        self.log("InputSelect {}: clearing timer".format(self.name))
        if self.handle is not None:
            self.cancel_timer(self.handle)
            self.handle = None
            self.publish_next_change(None, None)
        if state != self.default_value:
            self.log("InputSelect {}: scheduling reset".format(self.name))
            self.handle = self.run_once(self.on_reset, self.default_reset_time)
            self.publish_next_change(self.default_reset_time, self.default_value)

    def publish_next_change(self, time, value):
        self.set_state("sensor.{}_scheduled_change_time".format(self.name), state=self.parse_datetime(time) if time is not None else "undefined")
        self.set_state("sensor.{}_scheduled_next_value".format(self.name), state=value if value is not None else "undefined")

    def on_reset(self, kwargs):
        self.log("InputSelect {}: resetting ... ".format(self.name))
        self.call_service("input_select/select_option", entity_id=self.entity_id, option=self.default_value)