# Web-based speaker timer

This is a web-based speaker timer, that has a web server that can accept multiple clients at different `/name` URLs.  Pages are responsive, and changes instantly sync to the server.

As a basic usage, there is a web timer for one event that counts down until an admin resets the timer (admin mode shortcut for one event).  However, there is support for pre-programming multiple events and each counts down, and once a new event starts, the old one disappears from the display view.  This way a session organizer can pre-plan the day and the timer runs automatically.  Sessions expire 31 days after last use (or of course on server restart).


## Data model

There are **sessions** at URL `/SESSION_NAME/`.  Simply navigating to a URL returns the empty session and making some active changes in the browser creates a session that gets synced.  Multiple admins (and displays) should be able to access at the same time and updates get pushed to all connected clients dynamically and immediately.

The server holds canonical state, and any old state by clients is ignored.  Add a generation counter to detect old state by clients.  State is fully dynamic.  Admin clients shouldn't have old state, since updates get pushed to them immediately (if someone is currently editing, sorry).  So, if the state changes under an admin, it gets a push with the new generation number.  Any updates with this old state get rejected (should be infrequent - only on network difficulties) with a message to the admin and being told to refresh.

Each session has multiple **events** which have a {name (optional), start_time, duration}.  Of course, there can be UI for setting other things, for example duration and end time.  Times are stored as unixtime and timezone for input/display is inferred from the browser.


## Interface

### Users

There are no users.  Security is by hard-to-guess URLs.

### Admin

Allows one to view the events in the session and modify them.  There are controls to set and update combinations of start, end, duration, remaining time.  These controls will be refined later, choose something for now that makes sense.

* Table-like view
* Add event
* Delete event
* Set event start / end / duration / remaining times (not affecting other events)
* Increase/decrease duration of event (also then delaying subsequent events, as found by start time)
* Increase/decrease start time of event (also then delaying subsequent events)
* Highlight overlapping events but not otherwise blocking them.
* Quick "event now" setup.
* Download events as csv (start_time_unixtime,duration_sec,name)
* Upload events as csv (replacing all current)


### Display (`/SESSION_NAME/display`)

Read-only.  Show a countdown timer until end time and progress bar along the bottom.  If there is no current event, it's just blank.  If there are overlapping events, it shows the one that started most recently.

The countdown progress bar takes the bottom 20% of the screen and slowly moves from left to right until it covers the whole duration.  It starts green, then turns yellow at 25% left, then red at 15% left.  After expiry, the counter continues to count up negative numbers while flashing (2Hz), the progress bar flashes (2Hz), until the next event begins.


## Tools

Python, simple web server that listens on a port.  SSL etc. will be handled in another layer.  If a framework is needed, use FastAPI.  Websockets are made to push updates to all connected clients.  In-memory state means there must be one worker.

The server currently only stores sessions in memory and does not save anything to disk.

Design with simplicity and security in mind.
