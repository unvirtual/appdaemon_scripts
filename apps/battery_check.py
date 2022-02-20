import hassapi as hass
import datetime
class BatteryCheck(hass.Hass):

    def initialize(self):
        time = datetime.time(6, 00, 0)
        self.run_daily(self.check_batteries, time) 
        
    def check_batteries(self, kwargs):
        self.log("Battery checked")
        devices = self.get_state()
        values = {}
        low = []
        for device in devices:        
            battery = None
            if "group" not in device:
                try:
                    if "battery" in devices[device]["attributes"]:
                        battery = devices[device]["attributes"]["battery"]
                    if "battery_level" in devices[device]["attributes"]:
                        battery = devices[device]["attributes"]["battery_level"]
                    if device.endswith("battery_level") or device.endswith("battery"):
                        battery = float(self.get_state(device))
                except TypeError:
                    self.error("{} is not scriptable.".format(device))

            if battery != None:
                try:
                    friendly_name = self.get_state(device, attribute="group")['group.battery_group']['friendly_name']
                except TypeError:
                    friendly_name = self.get_state(device, attribute="friendly_name")

                blacklisted = False
                for skip in self.args["friendly_name_blacklist"]:
                    if skip in friendly_name:
                        blacklisted = True
                if blacklisted:
                    continue

                if battery < float(self.args["threshold"]):
                    low.append(friendly_name)
                values[friendly_name] = battery
        
        message = ""
        if low:
            for device in low:
                message = message + device + " \n"
        
        if low or ("always_send" in self.args and self.args["always_send"] == "1") or ("force" in kwargs and kwargs["force"] == 1):
            title = "WARNING: Battery low (below {}%)".format(self.args["threshold"])  
            self.call_service('notify/notify', title=title, message=message)
            self.call_service('persistent_notification/create', title=title, message=message)
            self.log("WARNING: Batteries below threshold {}".format(self.args["threshold"]), level="WARNING")
            self.log(message, level="WARNING")
        else:
            self.log("All good, No batteries below threshold {}".format(self.args["threshold"]))