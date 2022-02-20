import hassapi as hass
import datetime

class SensorHealth(hass.Hass):
    def initialize(self):
        self.run_every(self.check_health, "now", self.args["interval_seconds"])

    def check_health(self, kwargs):
        start_time = datetime.datetime.now() - datetime.timedelta(seconds = self.args["interval_seconds"])
        history = self.get_history(entity_id=self.args["entity_id"], start_time=start_time)
        history = [item for sublist in history for item in sublist]
        if len(history) == 0:
            self.log("WARNING: sensor {} timed out. Last measurement more then {} ago, threshold {} seconds".format(self.args["entity_id"], self.args["interval_seconds"], self.args["timeout_seconds"]), level="WARNING")
            return

        last_event = history[-1]
        last_changed = self.convert_utc(last_event["last_changed"])

        delta = self.get_now() - last_changed
        self.log("Check {}: last read {} seconds ago".format(self.args["entity_id"], int(delta.total_seconds())))
        if delta.total_seconds() > self.args["timeout_seconds"]:
            message = "sensor {} timed out. Last measurement at {} ({} seconds ago), threshold {} seconds".format(self.args["entity_id"], last_event["last_changed"], int(delta.total_seconds()), self.args["timeout_seconds"])
            self.log("WARNING: " + message, level="WARNING")
            self.call_service('notify/notify', title="WARNING: sensor timed out", message=message)
            self.call_service('persistent_notification/create', title="WARNING: sensor timed out", message=message)