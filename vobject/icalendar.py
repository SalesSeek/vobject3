"""Definitions and behavior for iCalendar, also known as vCalendar 2.0"""

import string
import behavior
import dateutil.rrule
import dateutil.tz
import StringIO
import datetime
import socket, random #for generating a UID
import itertools

from base import VObjectError, NativeError, ValidateError, ParseError, \
                    VBase, Component, ContentLine, logger, defaultSerialize, \
                    registerBehavior, backslashEscape, foldOneLine, \
                    newFromBehavior, CRLF, LF

#------------------------------- Constants -------------------------------------
DATENAMES = ("rdate", "exdate")
RULENAMES = ("exrule", "rrule")
DATESANDRULES = ("exrule", "rrule", "rdate", "exdate")
PRODID = u"-//PYVOBJECT//NONSGML Version 1//EN"

WEEKDAYS = "MO", "TU", "WE", "TH", "FR", "SA", "SU"
FREQUENCIES = ('YEARLY', 'MONTHLY', 'WEEKLY', 'DAILY', 'HOURLY', 'MINUTELY',
               'SECONDLY')

zeroDelta = datetime.timedelta(0)
twoHours  = datetime.timedelta(hours=2)

#---------------------------- TZID registry ------------------------------------
__tzidMap={}

def registerTzid(tzid, tzinfo):
    """Register a tzid -> tzinfo mapping."""
    __tzidMap[tzid]=tzinfo

def getTzid(tzid):
    """Return the tzid if it exists, or None."""
    return __tzidMap.get(tzid, None)

utc = dateutil.tz.tzutc()
registerTzid("UTC", utc)

#-------------------- Helper subclasses ----------------------------------------

class TimezoneComponent(Component):
    """A VTIMEZONE object.
    
    VTIMEZONEs are parsed by dateutil.tz.tzical, the resulting datetime.tzinfo
    subclass is stored in self.tzinfo, self.tzid stores the TZID associated
    with this timezone.
    
    @ivar name:
        The uppercased name of the object, in this case always 'VTIMEZONE'.
    @ivar tzinfo:
        A datetime.tzinfo subclass representing this timezone.
    @ivar tzid:
        The string used to refer to this timezone.
    
    """    
    def __init__(self, tzinfo=None, *args, **kwds):
        """Accept an existing Component or a tzinfo class."""
        super(TimezoneComponent, self).__init__(*args, **kwds)
        self.isNative=True
        # hack to make sure a behavior is assigned
        if self.behavior is None:
            self.behavior = VTimezone
        if tzinfo is not None:
            self.tzinfo = tzinfo
        if not hasattr(self, 'name') or self.name == '':
            self.name = 'VTIMEZONE'
            self.useBegin = True

    @classmethod
    def registerTzinfo(obj, tzinfo):
        """Register tzinfo if it's not already registered, return its tzid."""
        tzid = obj.pickTzid(tzinfo)
        if tzid and not getTzid(tzid):
            registerTzid(tzid, tzinfo)
        return tzid

    def gettzinfo(self):
        # workaround for dateutil failing to parse some experimental properties
        good_lines = ('rdate', 'rrule', 'dtstart', 'tzname', 'tzoffsetfrom',
                      'tzoffsetto', 'tzid')
        buffer = StringIO.StringIO()
        # allow empty VTIMEZONEs
        if len(self.contents) == 0:
            return None
        def customSerialize(obj):
            if isinstance(obj, Component):
                foldOneLine(buffer, u"BEGIN:" + obj.name)
                for child in obj.lines():
                    if child.name.lower() in good_lines:
                        child.serialize(buffer, 75, validate=False)
                for comp in obj.components():
                    customSerialize(comp)
                foldOneLine(buffer, u"END:" + obj.name)
        customSerialize(self)
        return dateutil.tz.tzical(StringIO.StringIO(str(buffer.getvalue()))).get()

    def settzinfo(self, tzinfo, start=2000, end=2030):
        """Create appropriate objects in self to represent tzinfo.
        
        Collapse DST transitions to rrules as much as possible.
        
        Assumptions:
        - DST <-> Standard transitions occur on the hour
        - never within a month of one another
        - twice or fewer times a year
        - never in the month of December
        - DST always moves offset exactly one hour later
        - tzinfo classes dst method always treats times that could be in either
          offset as being in the later regime
        
        """  
        def fromLastWeek(dt):
            """How many weeks from the end of the month dt is, starting from 1."""
            weekDelta = datetime.timedelta(weeks=1)
            n = 1
            current = dt + weekDelta
            while current.month == dt.month:
                n += 1
                current += weekDelta
            return n
        
        # lists of dictionaries defining rules which are no longer in effect
        completed = {'daylight' : [], 'standard' : []}
    
        # dictionary defining rules which are currently in effect
        working   = {'daylight' : None, 'standard' : None}
        
        # rule may be based on the nth week of the month or the nth from the last
        for year in xrange(start, end + 1):
            newyear = datetime.datetime(year, 1, 1)
            for transitionTo in 'daylight', 'standard':
                transition = getTransition(transitionTo, year, tzinfo)
                oldrule = working[transitionTo]
    
                if transition == newyear:
                    # transitionTo is in effect for the whole year
                    rule = {'end'        : None,
                            'start'      : newyear,
                            'month'      : 1,
                            'weekday'    : None,
                            'hour'       : None,
                            'plus'       : None,
                            'minus'      : None,
                            'name'       : tzinfo.tzname(newyear),
                            'offset'     : tzinfo.utcoffset(newyear),
                            'offsetfrom' : tzinfo.utcoffset(newyear)}
                    if oldrule is None:
                        # transitionTo was not yet in effect
                        working[transitionTo] = rule
                    else:
                        # transitionTo was already in effect
                        if (oldrule['offset'] != 
                            tzinfo.utcoffset(newyear)):
                            # old rule was different, it shouldn't continue
                            oldrule['end'] = year - 1
                            completed[transitionTo].append(oldrule)
                            working[transitionTo] = rule
                elif transition is None:
                    # transitionTo is not in effect
                    if oldrule is not None:
                        # transitionTo used to be in effect
                        oldrule['end'] = year - 1
                        completed[transitionTo].append(oldrule)
                        working[transitionTo] = None
                else:
                    # an offset transition was found
                    old_offset = tzinfo.utcoffset(transition - twoHours)
                    rule = {'end'     : None, # None, or an integer year
                            'start'   : transition, # the datetime of transition
                            'month'   : transition.month,
                            'weekday' : transition.weekday(),
                            'hour'    : transition.hour,
                            'name'    : tzinfo.tzname(transition),
                            'plus'    : (transition.day - 1)/ 7 + 1,#nth week of the month
                            'minus'   : fromLastWeek(transition), #nth from last week
                            'offset'  : tzinfo.utcoffset(transition), 
                            'offsetfrom' : old_offset}
        
                    if oldrule is None: 
                        working[transitionTo] = rule
                    else:
                        plusMatch  = rule['plus']  == oldrule['plus'] 
                        minusMatch = rule['minus'] == oldrule['minus'] 
                        truth = plusMatch or minusMatch
                        for key in 'month', 'weekday', 'hour', 'offset':
                            truth = truth and rule[key] == oldrule[key]
                        if truth:
                            # the old rule is still true, limit to plus or minus
                            if not plusMatch:
                                oldrule['plus'] = None
                            if not minusMatch:
                                oldrule['minus'] = None
                        else:
                            # the new rule did not match the old
                            oldrule['end'] = year - 1
                            completed[transitionTo].append(oldrule)
                            working[transitionTo] = rule
    
        for transitionTo in 'daylight', 'standard':
            if working[transitionTo] is not None:
                completed[transitionTo].append(working[transitionTo])
    
        self.tzid = []
        self.daylight = []
        self.standard = []
        
        self.add('tzid').value = self.pickTzid(tzinfo, True)
        
        old = None
        for transitionTo in 'daylight', 'standard':
            for rule in completed[transitionTo]:
                comp = self.add(transitionTo)
                dtstart = comp.add('dtstart')
                dtstart.value = rule['start']
                if rule['name'] is not None:
                    comp.add('tzname').value  = rule['name']
                line = comp.add('tzoffsetto')
                line.value = deltaToOffset(rule['offset'])
                line = comp.add('tzoffsetfrom')
                line.value = deltaToOffset(rule['offsetfrom'])
    
                if rule['plus'] is not None:
                    num = rule['plus']
                elif rule['minus'] is not None:
                    num = -1 * rule['minus']
                else:
                    num = None
                if num is not None:
                    dayString = ";BYDAY=" + str(num) + WEEKDAYS[rule['weekday']]
                else:
                    dayString = ""
                if rule['end'] is not None:
                    if rule['hour'] is None:
                        # all year offset, with no rule
                        endDate = datetime.datetime(rule['end'], 1, 1)
                    else:
                        weekday = dateutil.rrule.weekday(rule['weekday'], num)
                        du_rule = dateutil.rrule.rrule(dateutil.rrule.YEARLY,
                                   bymonth = rule['month'],byweekday = weekday,
                                   dtstart = datetime.datetime(
                                       rule['end'], 1, 1, rule['hour'])
                                  )
                        endDate = du_rule[0]
                    endDate = endDate.replace(tzinfo = utc) - rule['offsetfrom']
                    endString = ";UNTIL="+ dateTimeToString(endDate)
                else:
                    endString = ''
                rulestring = "FREQ=YEARLY%s;BYMONTH=%s%s" % \
                              (dayString, str(rule['month']), endString)
                
                comp.add('rrule').value = rulestring

    tzinfo = property(gettzinfo, settzinfo)
    # prevent Component's __setattr__ from overriding the tzinfo property
    normal_attributes = Component.normal_attributes + ['tzinfo']

    @staticmethod
    def pickTzid(tzinfo, allowUTC=False):
        """
        Given a tzinfo class, use known APIs to determine TZID, or use tzname.
        """
        if tzinfo is None or (not allowUTC and tzinfo_eq(tzinfo, utc)):
            #If tzinfo is UTC, we don't need a TZID
            return None
        # try PyICU's tzid key
        if hasattr(tzinfo, 'tzid'):
            return tzinfo.tzid
            
        # try pytz zone key
        if hasattr(tzinfo, 'zone'):
            return tzinfo.zone

        # try tzical's tzid key
        elif hasattr(tzinfo, '_tzid'):
            return tzinfo._tzid
        else:
            # return tzname for standard (non-DST) time
            notDST = datetime.timedelta(0)
            for month in xrange(1,13):
                dt = datetime.datetime(2000, month, 1)
                if tzinfo.dst(dt) == notDST:
                    return tzinfo.tzname(dt)
        # there was no standard time in 2000!
        raise VObjectError("Unable to guess TZID for tzinfo %s" % str(tzinfo))

    def __str__(self):
        return "<VTIMEZONE | " + str(getattr(self, 'tzid', 'No TZID')) +">"
    
    def __repr__(self):
        return self.__str__()
    
    def prettyPrint(self, level, tabwidth):
        pre = ' ' * level * tabwidth
        print pre, self.name
        print pre, "TZID:", self.tzid
        print

class RecurringComponent(Component):
    """A vCalendar component like VEVENT or VTODO which may recur.
        
    Any recurring component can have one or multiple RRULE, RDATE,
    EXRULE, or EXDATE lines, and one or zero DTSTART lines.  It can also have a
    variety of children that don't have any recurrence information.  
    
    In the example below, note that dtstart is included in the rruleset.
    This is not the default behavior for dateutil's rrule implementation unless
    dtstart would already have been a member of the recurrence rule, and as a
    result, COUNT is wrong. This can be worked around when getting rruleset by
    adjusting count down by one if an rrule has a count and dtstart isn't in its
    result set, but by default, the rruleset property doesn't do this work
    around, to access it getrruleset must be called with addRDate set True.
    
    >>> import dateutil.rrule, datetime
    >>> vevent = RecurringComponent(name='VEVENT')
    >>> vevent.add('rrule').value =u"FREQ=WEEKLY;COUNT=2;INTERVAL=2;BYDAY=TU,TH"
    >>> vevent.add('dtstart').value = datetime.datetime(2005, 1, 19, 9)
    
    When creating rrule's programmatically it should be kept in
    mind that count doesn't necessarily mean what rfc2445 says.
    
    >>> list(vevent.rruleset)
    [datetime.datetime(2005, 1, 20, 9, 0), datetime.datetime(2005, 2, 1, 9, 0)]
    >>> list(vevent.getrruleset(addRDate=True))
    [datetime.datetime(2005, 1, 19, 9, 0), datetime.datetime(2005, 1, 20, 9, 0)]
    
    Also note that dateutil will expand all-day events (datetime.date values) to
    datetime.datetime value with time 0 and no timezone.
    
    >>> vevent.dtstart.value = datetime.date(2005,3,18)
    >>> list(vevent.rruleset)
    [datetime.datetime(2005, 3, 29, 0, 0), datetime.datetime(2005, 3, 31, 0, 0)]
    >>> list(vevent.getrruleset(True))
    [datetime.datetime(2005, 3, 18, 0, 0), datetime.datetime(2005, 3, 29, 0, 0)]
    
    @ivar rruleset:
        A U{rruleset<https://moin.conectiva.com.br/DateUtil>}.
    """
    def __init__(self, *args, **kwds):
        super(RecurringComponent, self).__init__(*args, **kwds)
        self.isNative=True
        #self.clobberedRDates=[]


    def getrruleset(self, addRDate = False):
        """Get an rruleset created from self.
        
        If addRDate is True, add an RDATE for dtstart if it's not included in
        an RRULE, and count is decremented if it exists.
        
        Note that for rules which don't match DTSTART, DTSTART may not appear
        in list(rruleset), although it should.  By default, an RDATE is not
        created in these cases, and count isn't updated, so dateutil may list
        a spurious occurrence.
        
        """
        rruleset = None
        for name in DATESANDRULES:
            addfunc = None
            for line in self.contents.get(name, ()):
                # don't bother creating a rruleset unless there's a rule
                if rruleset is None:
                    rruleset = dateutil.rrule.rruleset()
                if addfunc is None:
                    addfunc=getattr(rruleset, name)

                if name in DATENAMES:
                    if type(line.value[0]) == datetime.datetime:
                        map(addfunc, line.value)
                    elif type(line.value[0]) == datetime.date:
                        for dt in line.value:
                            addfunc(datetime.datetime(dt.year, dt.month, dt.day))
                    else:
                        # ignore RDATEs with PERIOD values for now
                        pass
                elif name in RULENAMES:
                    try:
                        dtstart = self.dtstart.value
                    except AttributeError, KeyError:
                        # Special for VTODO - try DUE property instead
                        try:
                            if self.name == "VTODO":
                                dtstart = self.due.value
                            else:
                                # if there's no dtstart, just return None
                                return None
                        except AttributeError, KeyError:
                            # if there's no due, just return None
                            return None

                    # rrulestr complains about unicode, so cast to str
                    rule = dateutil.rrule.rrulestr(str(line.value),
                                                   dtstart=dtstart)
                    until = rule._until 
                    if until is not None and \
                       isinstance(dtstart, datetime.datetime) and \
                       (until.tzinfo != dtstart.tzinfo): 
                        # dateutil converts the UNTIL date to a datetime,
                        # check to see if the UNTIL parameter value was a date
                        vals = dict(pair.split('=') for pair in
                                    line.value.upper().split(';'))
                        if len(vals.get('UNTIL', '')) == 8:
                            until = datetime.datetime.combine(until.date(),
                                                              dtstart.time())
                        # While RFC2445 says UNTIL MUST be UTC, Chandler allows
                        # floating recurring events, and uses floating UNTIL values.
                        # Also, some odd floating UNTIL but timezoned DTSTART values
                        # have shown up in the wild, so put floating UNTIL values
                        # DTSTART's timezone
                        if until.tzinfo is None:
                            until = until.replace(tzinfo=dtstart.tzinfo)

                        if dtstart.tzinfo is not None:
                            until = until.astimezone(dtstart.tzinfo)

                        rule._until = until
                    
                    # add the rrule or exrule to the rruleset
                    addfunc(rule)
                    
                    if name == 'rrule' and addRDate:
                        try:
                            # dateutils does not work with all-day (datetime.date) items
                            # so we need to convert to a datetime.datetime
                            # (which is what dateutils does internally)
                            if not isinstance(dtstart, datetime.datetime):
                                adddtstart = datetime.datetime.fromordinal(dtstart.toordinal())
                            else:
                                adddtstart = dtstart
                            if rruleset._rrule[-1][0] != adddtstart:
                                rruleset.rdate(adddtstart)
                                added = True
                            else:
                                added = False
                        except IndexError:
                            # it's conceivable that an rrule might have 0 datetimes
                            added = False
                        if added and rruleset._rrule[-1]._count != None:
                            rruleset._rrule[-1]._count -= 1
        return rruleset

    def setrruleset(self, rruleset):
        
        # Get DTSTART from component (or DUE if no DTSTART in a VTODO)
        try:
            dtstart = self.dtstart.value
        except AttributeError, KeyError:
            if self.name == "VTODO":
                dtstart = self.due.value
            else:
                raise
            
        isDate = datetime.date == type(dtstart)
        if isDate:
            dtstart = datetime.datetime(dtstart.year,dtstart.month, dtstart.day)
            untilSerialize = dateToString
        else:
            # make sure to convert time zones to UTC
            untilSerialize = lambda x: dateTimeToString(x, True)

        for name in DATESANDRULES:
            if hasattr(self.contents, name):
                del self.contents[name]
            setlist = getattr(rruleset, '_' + name)
            if name in DATENAMES:
                setlist = list(setlist) # make a copy of the list
                if name == 'rdate' and dtstart in setlist:
                    setlist.remove(dtstart)
                if isDate:
                    setlist = [dt.date() for dt in setlist]
                if len(setlist) > 0:
                    self.add(name).value = setlist
            elif name in RULENAMES:
                for rule in setlist:
                    buf = StringIO.StringIO()
                    buf.write('FREQ=')
                    buf.write(FREQUENCIES[rule._freq])
                    
                    values = {}
                    
                    if rule._interval != 1:
                        values['INTERVAL'] = [str(rule._interval)]
                    if rule._wkst != 0: # wkst defaults to Monday
                        values['WKST'] = [WEEKDAYS[rule._wkst]]
                    if rule._bysetpos is not None:
                        values['BYSETPOS'] = [str(i) for i in rule._bysetpos]
                    
                    if rule._count is not None:
                        values['COUNT'] = [str(rule._count)]
                    elif rule._until is not None:
                        values['UNTIL'] = [untilSerialize(rule._until)]

                    days = []
                    if (rule._byweekday is not None and (
                                  dateutil.rrule.WEEKLY != rule._freq or 
                                   len(rule._byweekday) != 1 or 
                                rule._dtstart.weekday() != rule._byweekday[0])):
                        # ignore byweekday if freq is WEEKLY and day correlates
                        # with dtstart because it was automatically set by
                        # dateutil
                        days.extend(WEEKDAYS[n] for n in rule._byweekday)    
                        
                    if rule._bynweekday is not None:
                        days.extend(str(n) + WEEKDAYS[day] for day, n in rule._bynweekday)
                        
                    if len(days) > 0:
                        values['BYDAY'] = days 
                                                            
                    if rule._bymonthday is not None and len(rule._bymonthday) > 0:
                        if not (rule._freq <= dateutil.rrule.MONTHLY and
                                len(rule._bymonthday) == 1 and
                                rule._bymonthday[0] == rule._dtstart.day):
                            # ignore bymonthday if it's generated by dateutil
                            values['BYMONTHDAY'] = [str(n) for n in rule._bymonthday]

                    if rule._bymonth is not None and len(rule._bymonth) > 0:
                        if (rule._byweekday is not None or
                            len(rule._bynweekday or ()) > 0 or
                            not (rule._freq == dateutil.rrule.YEARLY and
                                 len(rule._bymonth) == 1 and
                                 rule._bymonth[0] == rule._dtstart.month)):
                            # ignore bymonth if it's generated by dateutil
                            values['BYMONTH'] = [str(n) for n in rule._bymonth]

                    if rule._byyearday is not None:
                        values['BYYEARDAY'] = [str(n) for n in rule._byyearday]
                    if rule._byweekno is not None:
                        values['BYWEEKNO'] = [str(n) for n in rule._byweekno]

                    # byhour, byminute, bysecond are always ignored for now

                    
                    for key, paramvals in values.iteritems():
                        buf.write(';')
                        buf.write(key)
                        buf.write('=')
                        buf.write(','.join(paramvals))

                    self.add(name).value = buf.getvalue()


            
    rruleset = property(getrruleset, setrruleset)

    def __setattr__(self, name, value):
        """For convenience, make self.contents directly accessible."""
        if name == 'rruleset':
            self.setrruleset(value)
        else:
            super(RecurringComponent, self).__setattr__(name, value)

class TextBehavior(behavior.Behavior):
    """Provide backslash escape encoding/decoding for single valued properties.
    
    TextBehavior also deals with base64 encoding if the ENCODING parameter is
    explicitly set to BASE64.
    
    """
    base64string = 'BASE64' # vCard uses B
    
    @classmethod
    def decode(cls, line):
        """Remove backslash escaping from line.value."""
        if line.encoded:
            encoding = getattr(line, 'encoding_param', None)
            if encoding and encoding.upper() == cls.base64string:
                line.value = line.value.decode('base64')
            else:
                line.value = stringToTextValues(line.value)[0]
            line.encoded=False
    
    @classmethod
    def encode(cls, line):
        """Backslash escape line.value."""
        if not line.encoded:
            encoding = getattr(line, 'encoding_param', None)
            if encoding and encoding.upper() == cls.base64string:
                line.value = line.value.encode('base64').replace('\n', '')
            else:
                line.value = backslashEscape(line.value)
            line.encoded=True

class VCalendarComponentBehavior(behavior.Behavior):
    defaultBehavior = TextBehavior
    isComponent = True

class RecurringBehavior(VCalendarComponentBehavior):
    """Parent Behavior for components which should be RecurringComponents."""
    hasNative = True
    
    @staticmethod
    def transformToNative(obj):
        """Turn a recurring Component into a RecurringComponent."""
        if not obj.isNative:
            object.__setattr__(obj, '__class__', RecurringComponent)
            obj.isNative = True
        return obj
    
    @staticmethod
    def transformFromNative(obj):
        if obj.isNative:
            object.__setattr__(obj, '__class__', Component)
            obj.isNative = False
        return obj
    
    @staticmethod        
    def generateImplicitParameters(obj):
        """Generate a UID if one does not exist.
        
        This is just a dummy implementation, for now.
        
        """
        if not hasattr(obj, 'uid'):
            rand = str(int(random.random() * 100000))
            now = datetime.datetime.now(utc)
            now = dateTimeToString(now)
            host = socket.gethostname()
            obj.add(ContentLine('UID', [], now + '-' + rand + '@' + host))        
            
    
class DateTimeBehavior(behavior.Behavior):
    """Parent Behavior for ContentLines containing one DATE-TIME."""
    hasNative = True

    @staticmethod
    def transformToNative(obj):
        """Turn obj.value into a datetime.

        RFC2445 allows times without time zone information, "floating times"
        in some properties.  Mostly, this isn't what you want, but when parsing
        a file, real floating times are noted by setting to 'TRUE' the
        X-VOBJ-FLOATINGTIME-ALLOWED parameter.

        """
        if obj.isNative: return obj
        obj.isNative = True
        if obj.value == '': return obj
        obj.value=str(obj.value)
        #we're cheating a little here, parseDtstart allows DATE
        obj.value=parseDtstart(obj)
        if obj.value.tzinfo is None:
            obj.params['X-VOBJ-FLOATINGTIME-ALLOWED'] = ['TRUE']
        if obj.params.get('TZID'):
            # Keep a copy of the original TZID around
            obj.params['X-VOBJ-ORIGINAL-TZID'] = obj.params['TZID']
            del obj.params['TZID']
        return obj

    @classmethod
    def transformFromNative(cls, obj):
        """Replace the datetime in obj.value with an ISO 8601 string."""
        if obj.isNative:
            obj.isNative = False
            tzid = TimezoneComponent.registerTzinfo(obj.value.tzinfo)
            obj.value = dateTimeToString(obj.value, cls.forceUTC)
            if not cls.forceUTC and tzid is not None:
                obj.tzid_param = tzid
            if obj.params.get('X-VOBJ-ORIGINAL-TZID'):
                if not hasattr(obj, 'tzid_param'):
                    obj.tzid_param = obj.params['X-VOBJ-ORIGINAL-TZID']
                del obj.params['X-VOBJ-ORIGINAL-TZID']

        return obj

class UTCDateTimeBehavior(DateTimeBehavior):
    """A value which must be specified in UTC."""
    forceUTC = True

class DateOrDateTimeBehavior(behavior.Behavior):
    """Parent Behavior for ContentLines containing one DATE or DATE-TIME."""
    hasNative = True

    @staticmethod
    def transformToNative(obj):
        """Turn obj.value into a date or datetime."""
        if obj.isNative: return obj
        obj.isNative = True
        if obj.value == '': return obj
        obj.value=str(obj.value)
        obj.value=parseDtstart(obj, allowSignatureMismatch=True)
        if getattr(obj, 'value_param', 'DATE-TIME').upper() == 'DATE-TIME':
            if hasattr(obj, 'tzid_param'):
                # Keep a copy of the original TZID around
                obj.params['X-VOBJ-ORIGINAL-TZID'] = obj.tzid_param
                del obj.tzid_param
        return obj

    @staticmethod
    def transformFromNative(obj):
        """Replace the date or datetime in obj.value with an ISO 8601 string."""
        if type(obj.value) == datetime.date:
            obj.isNative = False
            obj.value_param = 'DATE'
            obj.value = dateToString(obj.value)
            return obj
        else: return DateTimeBehavior.transformFromNative(obj)

class MultiDateBehavior(behavior.Behavior):
    """
    Parent Behavior for ContentLines containing one or more DATE, DATE-TIME, or
    PERIOD.
    
    """
    hasNative = True

    @staticmethod
    def transformToNative(obj):
        """
        Turn obj.value into a list of dates, datetimes, or
        (datetime, timedelta) tuples.
        
        """
        if obj.isNative:
            return obj
        obj.isNative = True
        if obj.value == '':
            obj.value = []
            return obj
        tzinfo = getTzid(getattr(obj, 'tzid_param', None))
        valueParam = getattr(obj, 'value_param', "DATE-TIME").upper()
        valTexts = obj.value.split(",")
        if valueParam == "DATE":
            obj.value = [stringToDate(x) for x in valTexts]
        elif valueParam == "DATE-TIME":
            obj.value = [stringToDateTime(x, tzinfo) for x in valTexts]
        elif valueParam == "PERIOD":
            obj.value = [stringToPeriod(x, tzinfo) for x in valTexts]
        return obj

    @staticmethod
    def transformFromNative(obj):
        """
        Replace the date, datetime or period tuples in obj.value with
        appropriate strings.
        
        """
        if obj.value and type(obj.value[0]) == datetime.date:
            obj.isNative = False
            obj.value_param = 'DATE'
            obj.value = ','.join([dateToString(val) for val in obj.value])
            return obj
        # Fixme: handle PERIOD case
        else:
            if obj.isNative:
                obj.isNative = False
                transformed = []
                tzid = None
                for val in obj.value:
                    if tzid is None and type(val) == datetime.datetime:
                        tzid = TimezoneComponent.registerTzinfo(val.tzinfo)
                        if tzid is not None:
                            obj.tzid_param = tzid
                    transformed.append(dateTimeToString(val))
                obj.value = ','.join(transformed)
            return obj

class MultiTextBehavior(behavior.Behavior):
    """Provide backslash escape encoding/decoding of each of several values.
    
    After transformation, value is a list of strings.
    
    """

    @staticmethod
    def decode(line):
        """Remove backslash escaping from line.value, then split on commas."""
        if line.encoded:
            line.value = stringToTextValues(line.value)
            line.encoded=False
    
    @staticmethod
    def encode(line):
        """Backslash escape line.value."""
        if not line.encoded:
            line.value = ','.join(backslashEscape(val) for val in line.value)
            line.encoded=True
    

#------------------------ Registered Behavior subclasses -----------------------
class VCalendar2_0(VCalendarComponentBehavior):
    """vCalendar 2.0 behavior. With added VAVAILABILITY support."""
    name = 'VCALENDAR'
    description = 'vCalendar 2.0, also known as iCalendar.'
    versionString = '2.0'
    sortFirst = ('version', 'calscale', 'method', 'prodid', 'vtimezone')
    knownChildren = {'CALSCALE':      (0, 1, None),#min, max, behaviorRegistry id
                     'METHOD':        (0, 1, None),
                     'VERSION':       (0, 1, None),#required, but auto-generated
                     'PRODID':        (1, 1, None),
                     'VTIMEZONE':     (0, None, None),
                     'VEVENT':        (0, None, None),
                     'VTODO':         (0, None, None),
                     'VJOURNAL':      (0, None, None),
                     'VFREEBUSY':     (0, None, None),
                     'VAVAILABILITY': (0, None, None),
                    }
                    
    @classmethod
    def generateImplicitParameters(cls, obj):
        """Create PRODID, VERSION, and VTIMEZONEs if needed.
        
        VTIMEZONEs will need to exist whenever TZID parameters exist or when
        datetimes with tzinfo exist.
        
        """
        for comp in obj.components():
            if comp.behavior is not None:
                comp.behavior.generateImplicitParameters(comp)
        if not hasattr(obj, 'prodid'):
            obj.add(ContentLine('PRODID', [], PRODID))
        if not hasattr(obj, 'version'):
            obj.add(ContentLine('VERSION', [], cls.versionString))
        tzidsUsed = {}

        def findTzids(obj, table):
            if isinstance(obj, ContentLine) and (obj.behavior is None or
                                                 not obj.behavior.forceUTC):
                if getattr(obj, 'tzid_param', None):
                    table[obj.tzid_param] = 1
                else:
                    if type(obj.value) == list:
                        for item in obj.value:
                            tzinfo = getattr(obj.value, 'tzinfo', None)
                            tzid = TimezoneComponent.registerTzinfo(tzinfo)
                            if tzid:
                                table[tzid] = 1
                    else:
                        tzinfo = getattr(obj.value, 'tzinfo', None)
                        tzid = TimezoneComponent.registerTzinfo(tzinfo)
                        if tzid:
                            table[tzid] = 1
            for child in obj.getChildren():
                if obj.name != 'VTIMEZONE':
                    findTzids(child, table)
        
        findTzids(obj, tzidsUsed)
        oldtzids = [x.tzid.value for x in getattr(obj, 'vtimezone_list', [])]
        for tzid in tzidsUsed.keys():
            if tzid != 'UTC' and tzid not in oldtzids:
                obj.add(TimezoneComponent(tzinfo=getTzid(tzid)))
registerBehavior(VCalendar2_0)

class VTimezone(VCalendarComponentBehavior):
    """Timezone behavior."""
    name = 'VTIMEZONE'
    hasNative = True
    description = 'A grouping of component properties that defines a time zone.'
    sortFirst = ('tzid', 'last-modified', 'tzurl', 'standard', 'daylight')
    knownChildren = {'TZID':         (1, 1, None),#min, max, behaviorRegistry id
                     'LAST-MODIFIED':(0, 1, None),
                     'TZURL':        (0, 1, None),
                     'STANDARD':     (0, None, None),#NOTE: One of Standard or
                     'DAYLIGHT':     (0, None, None) #      Daylight must appear
                    }

    @classmethod
    def validate(cls, obj, raiseException, *args):
        if not hasattr(obj, 'tzid') or obj.tzid.value is None:
            if raiseException:
                m = "VTIMEZONE components must contain a valid TZID"
                raise ValidateError(m)
            return False            
        if obj.contents.has_key('standard') or obj.contents.has_key('daylight'):
            return super(VTimezone, cls).validate(obj, raiseException, *args)
        else:
            if raiseException:
                m = "VTIMEZONE components must contain a STANDARD or a DAYLIGHT\
                     component"
                raise ValidateError(m)
            return False


    @staticmethod
    def transformToNative(obj):
        if not obj.isNative:
            object.__setattr__(obj, '__class__', TimezoneComponent)
            obj.isNative = True
            obj.registerTzinfo(obj.tzinfo)
        return obj

    @staticmethod
    def transformFromNative(obj):
        return obj

        
registerBehavior(VTimezone)

class DaylightOrStandard(VCalendarComponentBehavior):
    hasNative = False
    knownChildren = {'DTSTART':      (1, 1, None),#min, max, behaviorRegistry id
                     'RRULE':        (0, 1, None)}

registerBehavior(DaylightOrStandard, 'STANDARD')
registerBehavior(DaylightOrStandard, 'DAYLIGHT')


class VEvent(RecurringBehavior):
    """Event behavior."""
    name='VEVENT'
    sortFirst = ('uid', 'recurrence-id', 'dtstart', 'duration', 'dtend')

    description='A grouping of component properties, and possibly including \
                 "VALARM" calendar components, that represents a scheduled \
                 amount of time on a calendar.'
    knownChildren = {'DTSTART':      (0, 1, None),#min, max, behaviorRegistry id
                     'CLASS':        (0, 1, None),  
                     'CREATED':      (0, 1, None),
                     'DESCRIPTION':  (0, 1, None),  
                     'GEO':          (0, 1, None),  
                     'LAST-MODIFIED':(0, 1, None),
                     'LOCATION':     (0, 1, None),  
                     'ORGANIZER':    (0, 1, None),  
                     'PRIORITY':     (0, 1, None),  
                     'DTSTAMP':      (0, 1, None),
                     'SEQUENCE':     (0, 1, None),  
                     'STATUS':       (0, 1, None),  
                     'SUMMARY':      (0, 1, None),                     
                     'TRANSP':       (0, 1, None),  
                     'UID':          (1, 1, None),  
                     'URL':          (0, 1, None),  
                     'RECURRENCE-ID':(0, 1, None),  
                     'DTEND':        (0, 1, None), #NOTE: Only one of DtEnd or
                     'DURATION':     (0, 1, None), #      Duration can appear
                     'ATTACH':       (0, None, None),
                     'ATTENDEE':     (0, None, None),
                     'CATEGORIES':   (0, None, None),
                     'COMMENT':      (0, None, None),
                     'CONTACT':      (0, None, None),
                     'EXDATE':       (0, None, None),
                     'EXRULE':       (0, None, None),
                     'REQUEST-STATUS': (0, None, None),
                     'RELATED-TO':   (0, None, None),
                     'RESOURCES':    (0, None, None),
                     'RDATE':        (0, None, None),
                     'RRULE':        (0, None, None),
                     'VALARM':       (0, None, None)
                    }

    @classmethod
    def validate(cls, obj, raiseException, *args):
        if obj.contents.has_key('dtend') and obj.contents.has_key('duration'):
            if raiseException:
                m = "VEVENT components cannot contain both DTEND and DURATION\
                     components"
                raise ValidateError(m)
            return False
        else:
            return super(VEvent, cls).validate(obj, raiseException, *args)
      
registerBehavior(VEvent)


class VTodo(RecurringBehavior):
    """To-do behavior."""
    name='VTODO'
    description='A grouping of component properties and possibly "VALARM" \
                 calendar components that represent an action-item or \
                 assignment.'
    knownChildren = {'DTSTART':      (0, 1, None),#min, max, behaviorRegistry id
                     'CLASS':        (0, 1, None),
                     'COMPLETED':    (0, 1, None),
                     'CREATED':      (0, 1, None),
                     'DESCRIPTION':  (0, 1, None),  
                     'GEO':          (0, 1, None),  
                     'LAST-MODIFIED':(0, 1, None),
                     'LOCATION':     (0, 1, None),  
                     'ORGANIZER':    (0, 1, None),  
                     'PERCENT':      (0, 1, None),  
                     'PRIORITY':     (0, 1, None),  
                     'DTSTAMP':      (0, 1, None),
                     'SEQUENCE':     (0, 1, None),  
                     'STATUS':       (0, 1, None),  
                     'SUMMARY':      (0, 1, None),
                     'UID':          (0, 1, None),  
                     'URL':          (0, 1, None),  
                     'RECURRENCE-ID':(0, 1, None),  
                     'DUE':          (0, 1, None), #NOTE: Only one of Due or
                     'DURATION':     (0, 1, None), #      Duration can appear
                     'ATTACH':       (0, None, None),
                     'ATTENDEE':     (0, None, None),
                     'CATEGORIES':   (0, None, None),
                     'COMMENT':      (0, None, None),
                     'CONTACT':      (0, None, None),
                     'EXDATE':       (0, None, None),
                     'EXRULE':       (0, None, None),
                     'REQUEST-STATUS': (0, None, None),
                     'RELATED-TO':   (0, None, None),
                     'RESOURCES':    (0, None, None),
                     'RDATE':        (0, None, None),
                     'RRULE':        (0, None, None),
                     'VALARM':       (0, None, None)
                    }

    @classmethod
    def validate(cls, obj, raiseException, *args):
        if obj.contents.has_key('due') and obj.contents.has_key('duration'):
            if raiseException:
                m = "VTODO components cannot contain both DUE and DURATION\
                     components"
                raise ValidateError(m)
            return False
        else:
            return super(VTodo, cls).validate(obj, raiseException, *args)
      
registerBehavior(VTodo)


class VJournal(RecurringBehavior):
    """Journal entry behavior."""
    name='VJOURNAL'
    knownChildren = {'DTSTART':      (0, 1, None),#min, max, behaviorRegistry id
                     'CLASS':        (0, 1, None),  
                     'CREATED':      (0, 1, None),
                     'DESCRIPTION':  (0, 1, None),  
                     'LAST-MODIFIED':(0, 1, None),
                     'ORGANIZER':    (0, 1, None),  
                     'DTSTAMP':      (0, 1, None),
                     'SEQUENCE':     (0, 1, None),  
                     'STATUS':       (0, 1, None),  
                     'SUMMARY':      (0, 1, None),                     
                     'UID':          (0, 1, None),  
                     'URL':          (0, 1, None),  
                     'RECURRENCE-ID':(0, 1, None),  
                     'ATTACH':       (0, None, None),
                     'ATTENDEE':     (0, None, None),
                     'CATEGORIES':   (0, None, None),
                     'COMMENT':      (0, None, None),
                     'CONTACT':      (0, None, None),
                     'EXDATE':       (0, None, None),
                     'EXRULE':       (0, None, None),
                     'REQUEST-STATUS': (0, None, None),
                     'RELATED-TO':   (0, None, None),
                     'RDATE':        (0, None, None),
                     'RRULE':        (0, None, None)
                    }
registerBehavior(VJournal)


class VFreeBusy(VCalendarComponentBehavior):
    """Free/busy state behavior.

    >>> vfb = newFromBehavior('VFREEBUSY')
    >>> vfb.add('uid').value = 'test'
    >>> vfb.add('dtstart').value = datetime.datetime(2006, 2, 16, 1, tzinfo=utc)
    >>> vfb.add('dtend').value   = vfb.dtstart.value + twoHours
    >>> vfb.add('freebusy').value = [(vfb.dtstart.value, twoHours / 2)]
    >>> vfb.add('freebusy').value = [(vfb.dtstart.value, vfb.dtend.value)]
    >>> print vfb.serialize()
    BEGIN:VFREEBUSY
    UID:test
    DTSTART:20060216T010000Z
    DTEND:20060216T030000Z
    FREEBUSY:20060216T010000Z/PT1H
    FREEBUSY:20060216T010000Z/20060216T030000Z
    END:VFREEBUSY

    """
    name='VFREEBUSY'
    description='A grouping of component properties that describe either a \
                 request for free/busy time, describe a response to a request \
                 for free/busy time or describe a published set of busy time.'
    sortFirst = ('uid', 'dtstart', 'duration', 'dtend')
    knownChildren = {'DTSTART':      (0, 1, None),#min, max, behaviorRegistry id
                     'CONTACT':      (0, 1, None),
                     'DTEND':        (0, 1, None),
                     'DURATION':     (0, 1, None),
                     'ORGANIZER':    (0, 1, None),  
                     'DTSTAMP':      (0, 1, None), 
                     'UID':          (0, 1, None),  
                     'URL':          (0, 1, None),   
                     'ATTENDEE':     (0, None, None),
                     'COMMENT':      (0, None, None),
                     'FREEBUSY':     (0, None, None),
                     'REQUEST-STATUS': (0, None, None)
                    }
registerBehavior(VFreeBusy)


class VAlarm(VCalendarComponentBehavior):
    """Alarm behavior."""
    name='VALARM'
    description='Alarms describe when and how to provide alerts about events \
                 and to-dos.'
    knownChildren = {'ACTION':       (1, 1, None),#min, max, behaviorRegistry id
                     'TRIGGER':      (1, 1, None),  
                     'DURATION':     (0, 1, None),
                     'REPEAT':       (0, 1, None),
                     'DESCRIPTION':  (0, 1, None)
                    }

    @staticmethod
    def generateImplicitParameters(obj):
        """Create default ACTION and TRIGGER if they're not set."""
        try:
            obj.action
        except AttributeError:
            obj.add('action').value = 'AUDIO'
        try:
            obj.trigger
        except AttributeError:
            obj.add('trigger').value = datetime.timedelta(0)


    @classmethod
    def validate(cls, obj, raiseException, *args):
        """
        #TODO
     audioprop  = 2*(

                ; 'action' and 'trigger' are both REQUIRED,
                ; but MUST NOT occur more than once

                action / trigger /

                ; 'duration' and 'repeat' are both optional,
                ; and MUST NOT occur more than once each,
                ; but if one occurs, so MUST the other

                duration / repeat /

                ; the following is optional,
                ; but MUST NOT occur more than once

                attach /

     dispprop   = 3*(

                ; the following are all REQUIRED,
                ; but MUST NOT occur more than once

                action / description / trigger /

                ; 'duration' and 'repeat' are both optional,
                ; and MUST NOT occur more than once each,
                ; but if one occurs, so MUST the other

                duration / repeat /

     emailprop  = 5*(

                ; the following are all REQUIRED,
                ; but MUST NOT occur more than once

                action / description / trigger / summary

                ; the following is REQUIRED,
                ; and MAY occur more than once

                attendee /

                ; 'duration' and 'repeat' are both optional,
                ; and MUST NOT occur more than once each,
                ; but if one occurs, so MUST the other

                duration / repeat /

     procprop   = 3*(

                ; the following are all REQUIRED,
                ; but MUST NOT occur more than once

                action / attach / trigger /

                ; 'duration' and 'repeat' are both optional,
                ; and MUST NOT occur more than once each,
                ; but if one occurs, so MUST the other

                duration / repeat /

                ; 'description' is optional,
                ; and MUST NOT occur more than once

                description /
        if obj.contents.has_key('dtend') and obj.contents.has_key('duration'):
            if raiseException:
                m = "VEVENT components cannot contain both DTEND and DURATION\
                     components"
                raise ValidateError(m)
            return False
        else:
            return super(VEvent, cls).validate(obj, raiseException, *args)
        """
        return True
    
registerBehavior(VAlarm)

class VAvailability(VCalendarComponentBehavior):
    """Availability state behavior.

    >>> vav = newFromBehavior('VAVAILABILITY')
    >>> vav.add('uid').value = 'test'
    >>> vav.add('dtstamp').value = datetime.datetime(2006, 2, 15, 0, tzinfo=utc)
    >>> vav.add('dtstart').value = datetime.datetime(2006, 2, 16, 0, tzinfo=utc)
    >>> vav.add('dtend').value   = datetime.datetime(2006, 2, 17, 0, tzinfo=utc)
    >>> vav.add('busytype').value = "BUSY"
    >>> av = newFromBehavior('AVAILABLE')
    >>> av.add('uid').value = 'test1'
    >>> av.add('dtstamp').value = datetime.datetime(2006, 2, 15, 0, tzinfo=utc)
    >>> av.add('dtstart').value = datetime.datetime(2006, 2, 16, 9, tzinfo=utc)
    >>> av.add('dtend').value   = datetime.datetime(2006, 2, 16, 12, tzinfo=utc)
    >>> av.add('summary').value = "Available in the morning"
    >>> ignore = vav.add(av)
    >>> print vav.serialize()
    BEGIN:VAVAILABILITY
    UID:test
    DTSTART:20060216T000000Z
    DTEND:20060217T000000Z
    BEGIN:AVAILABLE
    UID:test1
    DTSTART:20060216T090000Z
    DTEND:20060216T120000Z
    DTSTAMP:20060215T000000Z
    SUMMARY:Available in the morning
    END:AVAILABLE
    BUSYTYPE:BUSY
    DTSTAMP:20060215T000000Z
    END:VAVAILABILITY

    """
    name='VAVAILABILITY'
    description='A component used to represent a user\'s available time slots.'
    sortFirst = ('uid', 'dtstart', 'duration', 'dtend')
    knownChildren = {'UID':           (1, 1, None),#min, max, behaviorRegistry id
                     'DTSTAMP':       (1, 1, None),
                     'BUSYTYPE':      (0, 1, None),
                     'CREATED':       (0, 1, None),
                     'DTSTART':       (0, 1, None),
                     'LAST-MODIFIED': (0, 1, None),
                     'ORGANIZER':     (0, 1, None),
                     'SEQUENCE':      (0, 1, None),
                     'SUMMARY':       (0, 1, None),
                     'URL':           (0, 1, None),
                     'DTEND':         (0, 1, None),
                     'DURATION':      (0, 1, None),
                     'CATEGORIES':    (0, None, None),
                     'COMMENT':       (0, None, None),
                     'CONTACT':       (0, None, None),
                     'AVAILABLE':     (0, None, None),
                    }

    @classmethod
    def validate(cls, obj, raiseException, *args):
        if obj.contents.has_key('dtend') and obj.contents.has_key('duration'):
            if raiseException:
                m = "VAVAILABILITY components cannot contain both DTEND and DURATION\
                     components"
                raise ValidateError(m)
            return False
        else:
            return super(VAvailability, cls).validate(obj, raiseException, *args)
      
registerBehavior(VAvailability)

class Available(RecurringBehavior):
    """Event behavior."""
    name='AVAILABLE'
    sortFirst = ('uid', 'recurrence-id', 'dtstart', 'duration', 'dtend')

    description='Defines a period of time in which a user is normally available.'
    knownChildren = {'DTSTAMP':      (1, 1, None),#min, max, behaviorRegistry id
                     'DTSTART':      (1, 1, None),
                     'UID':          (1, 1, None),  
                     'DTEND':        (0, 1, None), #NOTE: One of DtEnd or
                     'DURATION':     (0, 1, None), #      Duration must appear, but not both
                     'CREATED':      (0, 1, None),
                     'LAST-MODIFIED':(0, 1, None),
                     'RECURRENCE-ID':(0, 1, None),  
                     'RRULE':        (0, 1, None),
                     'SUMMARY':      (0, 1, None),                     
                     'CATEGORIES':   (0, None, None),
                     'COMMENT':      (0, None, None),
                     'CONTACT':      (0, None, None),
                     'EXDATE':       (0, None, None),
                     'RDATE':        (0, None, None),
                    }

    @classmethod
    def validate(cls, obj, raiseException, *args):
        has_dtend = obj.contents.has_key('dtend')
        has_duration = obj.contents.has_key('duration')
        if has_dtend and has_duration:
            if raiseException:
                m = "AVAILABLE components cannot contain both DTEND and DURATION\
                     properties"
                raise ValidateError(m)
            return False
        elif not (has_dtend or has_duration):
            if raiseException:
                m = "AVAILABLE components must contain one of DTEND or DURATION\
                     properties"
                raise ValidateError(m)
            return False
        else:
            return super(Available, cls).validate(obj, raiseException, *args)
      
registerBehavior(Available)

class Duration(behavior.Behavior):
    """Behavior for Duration ContentLines.  Transform to datetime.timedelta."""
    name = 'DURATION'
    hasNative = True

    @staticmethod
    def transformToNative(obj):
        """Turn obj.value into a datetime.timedelta."""
        if obj.isNative: return obj
        obj.isNative = True
        obj.value=str(obj.value)
        if obj.value == '':
            return obj
        else:
            deltalist=stringToDurations(obj.value)
            #When can DURATION have multiple durations?  For now:
            if len(deltalist) == 1:
                obj.value = deltalist[0]
                return obj
            else:
                raise ParseError("DURATION must have a single duration string.")

    @staticmethod
    def transformFromNative(obj):
        """Replace the datetime.timedelta in obj.value with an RFC2445 string.
        """
        if not obj.isNative: return obj
        obj.isNative = False
        obj.value = timedeltaToString(obj.value)
        return obj
    
registerBehavior(Duration)

class Trigger(behavior.Behavior):
    """DATE-TIME or DURATION"""
    name='TRIGGER'
    description='This property specifies when an alarm will trigger.'
    hasNative = True
    forceUTC = True

    @staticmethod
    def transformToNative(obj):
        """Turn obj.value into a timedelta or datetime."""
        if obj.isNative: return obj
        value = getattr(obj, 'value_param', 'DURATION').upper()
        if hasattr(obj, 'value_param'):
            del obj.value_param
        if obj.value == '':
            obj.isNative = True
            return obj
        elif value  == 'DURATION':
            try:
                return Duration.transformToNative(obj)
            except ParseError:
                logger.warn("TRIGGER not recognized as DURATION, trying "
                             "DATE-TIME, because iCal sometimes exports "
                             "DATE-TIMEs without setting VALUE=DATE-TIME")
                try:
                    obj.isNative = False
                    dt = DateTimeBehavior.transformToNative(obj)
                    return dt
                except:
                    msg = "TRIGGER with no VALUE not recognized as DURATION " \
                          "or as DATE-TIME"
                    raise ParseError(msg)
        elif value == 'DATE-TIME':
            #TRIGGERs with DATE-TIME values must be in UTC, we could validate
            #that fact, for now we take it on faith.
            return DateTimeBehavior.transformToNative(obj)
        else:
            raise ParseError("VALUE must be DURATION or DATE-TIME")        

    @staticmethod
    def transformFromNative(obj):
        if type(obj.value) == datetime.datetime:
            obj.value_param = 'DATE-TIME'
            return UTCDateTimeBehavior.transformFromNative(obj)
        elif type(obj.value) == datetime.timedelta:
            return Duration.transformFromNative(obj)
        else:
            raise NativeError("Native TRIGGER values must be timedelta or datetime")

registerBehavior(Trigger)

class PeriodBehavior(behavior.Behavior):
    """A list of (date-time, timedelta) tuples.

    >>> line = ContentLine('test', [], '', isNative=True)
    >>> line.behavior = PeriodBehavior
    >>> line.value = [(datetime.datetime(2006, 2, 16, 10), twoHours)]
    >>> line.transformFromNative().value
    '20060216T100000/PT2H'
    >>> line.transformToNative().value
    [(datetime.datetime(2006, 2, 16, 10, 0), datetime.timedelta(0, 7200))]
    >>> line.value.append((datetime.datetime(2006, 5, 16, 10), twoHours))
    >>> print line.serialize().strip()
    TEST:20060216T100000/PT2H,20060516T100000/PT2H
    """
    hasNative = True
    
    @staticmethod
    def transformToNative(obj):
        """Convert comma separated periods into tuples."""
        if obj.isNative:
            return obj
        obj.isNative = True
        if obj.value == '':
            obj.value = []
            return obj
        tzinfo = getTzid(getattr(obj, 'tzid_param', None))
        obj.value = [stringToPeriod(x, tzinfo) for x in obj.value.split(",")]
        return obj
        
    @classmethod
    def transformFromNative(cls, obj):
        """Convert the list of tuples in obj.value to strings."""
        if obj.isNative:
            obj.isNative = False
            transformed = []
            for tup in obj.value:
                transformed.append(periodToString(tup, cls.forceUTC))
            if len(transformed) > 0:
                tzid = TimezoneComponent.registerTzinfo(tup[0].tzinfo)
                if not cls.forceUTC and tzid is not None:
                    obj.tzid_param = tzid
                            
            obj.value = ','.join(transformed)

        return obj

class FreeBusy(PeriodBehavior):
    """Free or busy period of time, must be specified in UTC."""
    name = 'FREEBUSY'
    forceUTC = True
registerBehavior(FreeBusy)

class RRule(behavior.Behavior):
    """
    Dummy behavior to avoid having RRULEs being treated as text lines (and thus
    having semi-colons inaccurately escaped).
    """
registerBehavior(RRule, 'RRULE')
registerBehavior(RRule, 'EXRULE')

#------------------------ Registration of common classes -----------------------

utcDateTimeList = ['LAST-MODIFIED', 'CREATED', 'COMPLETED', 'DTSTAMP']
map(lambda x: registerBehavior(UTCDateTimeBehavior, x), utcDateTimeList)

dateTimeOrDateList = ['DTEND', 'DTSTART', 'DUE', 'RECURRENCE-ID']
map(lambda x: registerBehavior(DateOrDateTimeBehavior, x),
    dateTimeOrDateList)
    
registerBehavior(MultiDateBehavior, 'RDATE')
registerBehavior(MultiDateBehavior, 'EXDATE')


textList = ['CALSCALE', 'METHOD', 'PRODID', 'CLASS', 'COMMENT', 'DESCRIPTION',
            'LOCATION', 'STATUS', 'SUMMARY', 'TRANSP', 'CONTACT', 'RELATED-TO',
            'UID', 'ACTION', 'REQUEST-STATUS', 'TZID', 'BUSYTYPE']
map(lambda x: registerBehavior(TextBehavior, x), textList)

multiTextList = ['CATEGORIES', 'RESOURCES']
map(lambda x: registerBehavior(MultiTextBehavior, x), multiTextList)

#------------------------ Serializing helper functions -------------------------

def numToDigits(num, places):
    """Helper, for converting numbers to textual digits."""
    s = str(num)
    if len(s) < places:
        return ("0" * (places - len(s))) + s
    elif len(s) > places:
        return s[len(s)-places: ]
    else:
        return s

def timedeltaToString(delta):
    """Convert timedelta to an rfc2445 DURATION."""
    if delta.days == 0: sign = 1
    else: sign = delta.days / abs(delta.days)
    delta = abs(delta)
    days = delta.days
    hours = delta.seconds / 3600
    minutes = (delta.seconds % 3600) / 60
    seconds = delta.seconds % 60
    out = ''
    if sign == -1: out = '-'
    out += 'P'
    if days: out += str(days) + 'D'
    if hours or minutes or seconds: out += 'T'
    elif not days: #Deal with zero duration
        out += 'T0S'
    if hours: out += str(hours) + 'H'
    if minutes: out += str(minutes) + 'M'
    if seconds: out += str(seconds) + 'S'
    return out

def timeToString(dateOrDateTime):
    """
    Wraps dateToString and dateTimeToString, returning the results
    of either based on the type of the argument
    """
    # Didn't use isinstance here as date and datetime sometimes evalutes as both
    if (type(dateOrDateTime) == datetime.date):
        return dateToString(dateOrDateTime)
    elif(type(dateOrDateTime) == datetime.datetime):
        return dateTimeToString(dateOrDateTime)
    

def dateToString(date):
    year  = numToDigits( date.year,  4 )
    month = numToDigits( date.month, 2 )
    day   = numToDigits( date.day,   2 )
    return year + month + day

def dateTimeToString(dateTime, convertToUTC=False):
    """Ignore tzinfo unless convertToUTC.  Output string."""
    if dateTime.tzinfo and convertToUTC:
        dateTime = dateTime.astimezone(utc)
    if tzinfo_eq(dateTime.tzinfo, utc): utcString = "Z"
    else: utcString = ""

    year  = numToDigits( dateTime.year,  4 )
    month = numToDigits( dateTime.month, 2 )
    day   = numToDigits( dateTime.day,   2 )
    hour  = numToDigits( dateTime.hour,  2 )
    mins  = numToDigits( dateTime.minute,  2 )
    secs  = numToDigits( dateTime.second,  2 )

    return year + month + day + "T" + hour + mins + secs + utcString

def deltaToOffset(delta):
    absDelta = abs(delta)
    hours = absDelta.seconds / 3600
    hoursString      = numToDigits(hours, 2)
    minutesString    = '00'
    if absDelta == delta:
        signString = "+"
    else:
        signString = "-"
    return signString + hoursString + minutesString

def periodToString(period, convertToUTC=False):
    txtstart = dateTimeToString(period[0], convertToUTC)
    if isinstance(period[1], datetime.timedelta):
        txtend = timedeltaToString(period[1])
    else:
        txtend = dateTimeToString(period[1], convertToUTC)
    return txtstart + "/" + txtend

#----------------------- Parsing functions -------------------------------------

def isDuration(s):
    s = string.upper(s)
    return (string.find(s, "P") != -1) and (string.find(s, "P") < 2)

def stringToDate(s):
    year  = int( s[0:4] )
    month = int( s[4:6] )
    day   = int( s[6:8] )
    return datetime.date(year,month,day)

def stringToDateTime(s, tzinfo=None):
    """Returns datetime.datetime object."""
    try:
        year   = int( s[0:4] )
        month  = int( s[4:6] )
        day    = int( s[6:8] )
        hour   = int( s[9:11] )
        minute = int( s[11:13] )
        second = int( s[13:15] )
        if len(s) > 15:
            if s[15] == 'Z':
                tzinfo = utc
    except:
        raise ParseError("'%s' is not a valid DATE-TIME" % s)
    return datetime.datetime(year, month, day, hour, minute, second, 0, tzinfo)


escapableCharList = "\\;,Nn"

def stringToTextValues(s, listSeparator=',', charList=None, strict=False):
    """Returns list of strings."""
    
    if charList is None:
        charList = escapableCharList

    def escapableChar (c):
        return c in charList

    def error(msg):
        if strict:
            raise ParseError(msg)
        else:
            #logger.error(msg)
            print msg

    #vars which control state machine
    charIterator = enumerate(s)
    state        = "read normal"

    current = ""
    results = []

    while True:
        try:
            charIndex, char = charIterator.next()
        except:
            char = "eof"

        if state == "read normal":
            if char == '\\':
                state = "read escaped char"
            elif char == listSeparator:
                state = "read normal"
                results.append(current)
                current = ""
            elif char == "eof":
                state = "end"
            else:
                state = "read normal"
                current = current + char

        elif state == "read escaped char":
            if escapableChar(char):
                state = "read normal"
                if char in 'nN': 
                    current = current + '\n'
                else:
                    current = current + char
            else:
                state = "read normal"
                # leave unrecognized escaped characters for later passes
                current = current + '\\' + char 

        elif state == "end":    #an end state
            if current != "" or len(results) == 0:
                results.append(current)
            return results

        elif state == "error":  #an end state
            return results

        else:
            state = "error"
            error("error: unknown state: '%s' reached in %s" % (state, s))

def stringToDurations(s, strict=False):
    """Returns list of timedelta objects."""
    def makeTimedelta(sign, week, day, hour, minute, sec):
        if sign == "-": sign = -1
        else: sign = 1
        week      = int(week)
        day       = int(day)
        hour      = int(hour)
        minute    = int(minute)
        sec       = int(sec)
        return sign * datetime.timedelta(weeks=week, days=day, hours=hour, minutes=minute, seconds=sec)

    def error(msg):
        if strict:
            raise ParseError(msg)
        else:
            raise ParseError(msg)
            #logger.error(msg)
    
    #vars which control state machine
    charIterator = enumerate(s)
    state        = "start"

    durations = []
    current   = ""
    sign      = None
    week      = 0
    day       = 0
    hour      = 0
    minute    = 0
    sec       = 0

    while True:
        try:
            charIndex, char = charIterator.next()
        except:
            charIndex += 1
            char = "eof"

        if state == "start":
            if char == '+':
                state = "start"
                sign = char
            elif char == '-':
                state = "start"
                sign = char
            elif char.upper() == 'P':
                state = "read field"
            elif char == "eof":
                state = "error"
                error("got end-of-line while reading in duration: " + s)
            elif char in string.digits:
                state = "read field"
                current = current + char   #update this part when updating "read field"
            else:
                state = "error"
                print "got unexpected character %s reading in duration: %s" % (char, s)
                error("got unexpected character %s reading in duration: %s" % (char, s))

        elif state == "read field":
            if (char in string.digits):
                state = "read field"
                current = current + char   #update part above when updating "read field"   
            elif char.upper() == 'T':
                state = "read field"
            elif char.upper() == 'W':
                state = "read field"
                week    = current
                current = ""
            elif char.upper() == 'D':
                state = "read field"
                day     = current
                current = ""
            elif char.upper() == 'H':
                state = "read field"
                hour    = current
                current = ""
            elif char.upper() == 'M':
                state = "read field"
                minute  = current
                current = ""
            elif char.upper() == 'S':
                state = "read field"
                sec     = current
                current = ""
            elif char == ",":
                state = "start"
                durations.append( makeTimedelta(sign, week, day, hour, minute, sec) )
                current   = ""
                sign      = None
                week      = None
                day       = None
                hour      = None
                minute    = None
                sec       = None  
            elif char == "eof":
                state = "end"
            else:
                state = "error"
                error("got unexpected character reading in duration: " + s)
            
        elif state == "end":    #an end state
            #print "stuff: %s, durations: %s" % ([current, sign, week, day, hour, minute, sec], durations)

            if (sign or week or day or hour or minute or sec):
                durations.append( makeTimedelta(sign, week, day, hour, minute, sec) )
            return durations

        elif state == "error":  #an end state
            error("in error state")
            return durations

        else:
            state = "error"
            error("error: unknown state: '%s' reached in %s" % (state, s))

def parseDtstart(contentline, allowSignatureMismatch=False):
    """Convert a contentline's value into a date or date-time.
    
    A variety of clients don't serialize dates with the appropriate VALUE
    parameter, so rather than failing on these (technically invalid) lines,
    if allowSignatureMismatch is True, try to parse both varieties.
    
    """
    tzinfo = getTzid(getattr(contentline, 'tzid_param', None))
    valueParam = getattr(contentline, 'value_param', 'DATE-TIME').upper()
    if valueParam == "DATE":
        return stringToDate(contentline.value)
    elif valueParam == "DATE-TIME":
        try:
            return stringToDateTime(contentline.value, tzinfo)
        except:
            if allowSignatureMismatch:
                return stringToDate(contentline.value)
            else:
                raise

def stringToPeriod(s, tzinfo=None):
    values   = string.split(s, "/")
    start = stringToDateTime(values[0], tzinfo)
    valEnd   = values[1]
    if isDuration(valEnd): #period-start = date-time "/" dur-value
        delta = stringToDurations(valEnd)[0]
        return (start, delta)
    else:
        return (start, stringToDateTime(valEnd, tzinfo))


def getTransition(transitionTo, year, tzinfo):
    """Return the datetime of the transition to/from DST, or None."""

    def firstTransition(iterDates, test):
        """
        Return the last date not matching test, or None if all tests matched.
        """
        success = None
        for dt in iterDates:
            if not test(dt):
                success = dt
            else:
                if success is not None:
                    return success
        return success # may be None

    def generateDates(year, month=None, day=None):
        """Iterate over possible dates with unspecified values."""
        months = range(1, 13)
        days   = range(1, 32)
        hours  = range(0, 24)
        if month is None:
            for month in months:
                yield datetime.datetime(year, month, 1)
        elif day is None:
            for day in days:
                try:
                    yield datetime.datetime(year, month, day)
                except ValueError:
                    pass
        else:
            for hour in hours:
                yield datetime.datetime(year, month, day, hour)

    assert transitionTo in ('daylight', 'standard')
    if transitionTo == 'daylight':
        def test(dt): return tzinfo.dst(dt) != zeroDelta
    elif transitionTo == 'standard':
        def test(dt): return tzinfo.dst(dt) == zeroDelta
    newyear = datetime.datetime(year, 1, 1)
    monthDt = firstTransition(generateDates(year), test)
    if monthDt is None:
        return newyear
    elif monthDt.month == 12:
        return None
    else:
        # there was a good transition somewhere in a non-December month
        month = monthDt.month
        day         = firstTransition(generateDates(year, month), test).day
        uncorrected = firstTransition(generateDates(year, month, day), test)
        if transitionTo == 'standard':
            # assuming tzinfo.dst returns a new offset for the first
            # possible hour, we need to add one hour for the offset change
            # and another hour because firstTransition returns the hour
            # before the transition
            return uncorrected + datetime.timedelta(hours=2)
        else:
            return uncorrected + datetime.timedelta(hours=1)

def tzinfo_eq(tzinfo1, tzinfo2, startYear = 2000, endYear=2020):
    """Compare offsets and DST transitions from startYear to endYear."""
    if tzinfo1 == tzinfo2:
        return True
    elif tzinfo1 is None or tzinfo2 is None:
        return False
    
    def dt_test(dt):
        if dt is None:
            return True
        return tzinfo1.utcoffset(dt) == tzinfo2.utcoffset(dt)

    if not dt_test(datetime.datetime(startYear, 1, 1)):
        return False
    for year in xrange(startYear, endYear):
        for transitionTo in 'daylight', 'standard':
            t1=getTransition(transitionTo, year, tzinfo1)
            t2=getTransition(transitionTo, year, tzinfo2)
            if t1 != t2 or not dt_test(t1):
                return False
    return True


#------------------- Testing and running functions -----------------------------
if __name__ == '__main__':
    import tests
    tests._test()