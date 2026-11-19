"""Indian cities, states and PIN codes — the reference the location
fixes and the card reader both lean on.

Two questions:

    state_for_city('Bharuch')  -> 'Gujarat'
    state_for_pin('392001')    -> 'Gujarat'

and one spelling rule, `canonical_state`, so "MH", "Maharashtra " and
"MAHARASHTRA" are one state.

Only cities this business actually works in, plus every state capital
and the large ports and industrial towns. A city that is not here
answers '' — the caller leaves the row alone rather than guessing.
A few names exist in two states (Aurangabad: Maharashtra and Bihar);
those are left OUT, because a wrong "fix" is worse than none.
"""
from __future__ import annotations

import re

STATES = (
    'Andhra Pradesh', 'Arunachal Pradesh', 'Assam', 'Bihar',
    'Chhattisgarh', 'Goa', 'Gujarat', 'Haryana', 'Himachal Pradesh',
    'Jharkhand', 'Karnataka', 'Kerala', 'Madhya Pradesh', 'Maharashtra',
    'Manipur', 'Meghalaya', 'Mizoram', 'Nagaland', 'Odisha', 'Punjab',
    'Rajasthan', 'Sikkim', 'Tamil Nadu', 'Telangana', 'Tripura',
    'Uttar Pradesh', 'Uttarakhand', 'West Bengal',
    'Andaman and Nicobar Islands', 'Chandigarh',
    'Dadra and Nagar Haveli and Daman and Diu', 'Delhi',
    'Jammu and Kashmir', 'Ladakh', 'Lakshadweep', 'Puducherry',
)

_STATE_ALIASES = {
    'ap': 'Andhra Pradesh', 'andhra': 'Andhra Pradesh',
    'as': 'Assam', 'br': 'Bihar', 'cg': 'Chhattisgarh',
    'chattisgarh': 'Chhattisgarh', 'chhatisgarh': 'Chhattisgarh',
    'ga': 'Goa', 'gj': 'Gujarat', 'guj': 'Gujarat', 'gujrat': 'Gujarat',
    'hr': 'Haryana', 'hp': 'Himachal Pradesh', 'jh': 'Jharkhand',
    'ka': 'Karnataka', 'kar': 'Karnataka', 'kl': 'Kerala',
    'mp': 'Madhya Pradesh', 'm.p.': 'Madhya Pradesh',
    'mh': 'Maharashtra', 'maha': 'Maharashtra', 'maharastra': 'Maharashtra',
    'od': 'Odisha', 'or': 'Odisha', 'orissa': 'Odisha',
    'pb': 'Punjab', 'rj': 'Rajasthan', 'tn': 'Tamil Nadu',
    'tamilnadu': 'Tamil Nadu', 'ts': 'Telangana', 'tg': 'Telangana',
    'up': 'Uttar Pradesh', 'u.p.': 'Uttar Pradesh',
    'uk': 'Uttarakhand', 'uttaranchal': 'Uttarakhand',
    'wb': 'West Bengal', 'westbengal': 'West Bengal',
    'dl': 'Delhi', 'new delhi': 'Delhi', 'nct of delhi': 'Delhi',
    'delhi ncr': 'Delhi', 'j&k': 'Jammu and Kashmir',
    'jammu & kashmir': 'Jammu and Kashmir', 'pondicherry': 'Puducherry',
    'daman': 'Dadra and Nagar Haveli and Daman and Diu',
    'silvassa': 'Dadra and Nagar Haveli and Daman and Diu',
    'dnh': 'Dadra and Nagar Haveli and Daman and Diu',
    'andaman': 'Andaman and Nicobar Islands',
}
_BY_LOWER = {s.lower(): s for s in STATES}


def canonical_state(value):
    """One spelling per state, or '' for something that is not one."""
    text = ' '.join(str(value or '').strip().lower().split()).strip('.,')
    if not text:
        return ''
    if text in _BY_LOWER:
        return _BY_LOWER[text]
    text2 = text.replace('&', 'and')
    if text2 in _BY_LOWER:
        return _BY_LOWER[text2]
    return _STATE_ALIASES.get(text, _STATE_ALIASES.get(text.replace(' ', ''), ''))


_CITIES = {
    'Maharashtra': (
        'Mumbai', 'Navi Mumbai', 'Thane', 'Bhiwandi', 'Kalyan', 'Panvel',
        'Pune', 'Pimpri', 'Chinchwad', 'Chakan', 'Talegaon', 'Ranjangaon',
        'Nagpur', 'Nashik', 'Waluj', 'Chhatrapati Sambhajinagar',
        'Solapur', 'Kolhapur', 'Satara', 'Sangli', 'Ahmednagar',
        'Jalgaon', 'Dhule', 'Amravati', 'Akola', 'Chandrapur', 'Nanded',
        'Latur', 'Ratnagiri', 'Tarapur', 'Boisar', 'Palghar', 'Raigad',
        'Taloja', 'Khopoli', 'Uran', 'JNPT', 'Nhava Sheva', 'Vasai',
        'Wardha', 'Butibori', 'Baramati', 'Shirur', 'Supa', 'Sinnar',
        'Igatpuri', 'Ambernath', 'Dombivli', 'Mahad', 'Roha', 'Dahej MH'),
    'Gujarat': (
        'Ahmedabad', 'Vadodara', 'Baroda', 'Surat', 'Rajkot', 'Bharuch',
        'Ankleshwar', 'Dahej', 'Hazira', 'Jamnagar', 'Kandla', 'Mundra',
        'Gandhidham', 'Kutch', 'Bhuj', 'Anjar', 'Gandhinagar', 'Sanand',
        'Halol', 'Savli', 'Mehsana', 'Vapi', 'Valsad', 'Navsari',
        'Bhavnagar', 'Pipavav', 'Morbi', 'Anand', 'Nadiad', 'Dholera',
        'Kalol', 'Mandal', 'Becharaji', 'Jhagadia', 'Sachin', 'Pithampur GJ',
        'Porbandar', 'Veraval', 'Junagadh', 'Alang', 'Sikka', 'Vadinar',
        'Okha', 'Magdalla', 'Dahod', 'Godhra', 'Himmatnagar', 'Palanpur'),
    'Tamil Nadu': (
        'Chennai', 'Coimbatore', 'Madurai', 'Tiruchirappalli', 'Trichy',
        'Salem', 'Tirunelveli', 'Tuticorin', 'Thoothukudi', 'Ennore',
        'Kattupalli', 'Hosur', 'Sriperumbudur', 'Oragadam', 'Ranipet',
        'Vellore', 'Erode', 'Tiruppur', 'Karur', 'Namakkal', 'Cuddalore',
        'Kanchipuram', 'Gummidipoondi', 'Ambattur', 'Sulur',
        'Thanjavur', 'Neyveli', 'Mettur', 'Pollachi', 'Krishnagiri'),
    'Karnataka': (
        'Bengaluru', 'Bangalore', 'Mysuru', 'Mysore', 'Mangaluru',
        'Mangalore', 'Hubballi', 'Hubli', 'Dharwad', 'Belagavi', 'Belgaum',
        'Ballari', 'Bellary', 'Tumakuru', 'Tumkur', 'Hosakote',
        'Nelamangala', 'Bidadi', 'Peenya', 'Kolar', 'Raichur', 'Udupi',
        'Davangere', 'Shivamogga', 'Hospet', 'Kalaburagi', 'Gulbarga',
        'Vijayapura', 'Bijapur', 'Karwar', 'Doddaballapur', 'Narasapura'),
    'Telangana': (
        'Hyderabad', 'Secunderabad', 'Warangal', 'Karimnagar',
        'Nizamabad', 'Khammam', 'Ramagundam', 'Patancheru', 'Medak',
        'Sangareddy', 'Shamshabad', 'Kothagudem', 'Mahbubnagar'),
    'Andhra Pradesh': (
        'Visakhapatnam', 'Vizag', 'Vishakapatnam', 'Vijayawada',
        'Guntur', 'Nellore', 'Kakinada', 'Krishnapatnam', 'Tirupati',
        'Kurnool', 'Anantapur', 'Kadapa', 'Rajahmundry', 'Ongole',
        'Gangavaram', 'Sri City', 'Srikakulam', 'Machilipatnam',
        'Eluru', 'Amaravati', 'Chittoor'),
    'Kerala': (
        'Kochi', 'Cochin', 'Ernakulam', 'Thiruvananthapuram',
        'Trivandrum', 'Kozhikode', 'Calicut', 'Thrissur', 'Kollam',
        'Kannur', 'Palakkad', 'Alappuzha', 'Vizhinjam', 'Kottayam'),
    'West Bengal': (
        'Kolkata', 'Calcutta', 'Howrah', 'Haldia', 'Durgapur', 'Asansol',
        'Siliguri', 'Kharagpur', 'Hooghly', 'Barasat', 'Salt Lake',
        'Kalyani', 'Bardhaman', 'Burdwan', 'Panagarh', 'Dankuni'),
    'Odisha': (
        'Bhubaneswar', 'Bhubaneshwar', 'Cuttack', 'Rourkela',
        'Sambalpur', 'Paradip', 'Jharsuguda', 'Angul', 'Kalinganagar',
        'Jajpur', 'Dhamra', 'Gopalpur', 'Berhampur', 'Balasore',
        'Keonjhar', 'Talcher', 'Jharsugda', 'Kirandul OD'),
    'Jharkhand': (
        'Ranchi', 'Jamshedpur', 'Dhanbad', 'Bokaro', 'Hazaribagh',
        'Deoghar', 'Giridih', 'Adityapur', 'Ramgarh'),
    'Bihar': ('Patna', 'Gaya', 'Bhagalpur', 'Muzaffarpur', 'Begusarai',
              'Darbhanga', 'Barauni'),
    'Chhattisgarh': (
        'Raipur', 'Bhilai', 'Durg', 'Bilaspur', 'Korba', 'Raigarh',
        'Kirandul', 'Bacheli', 'Dantewada', 'Jagdalpur', 'Naya Raipur'),
    'Madhya Pradesh': (
        'Bhopal', 'Indore', 'Jabalpur', 'Gwalior', 'Ujjain', 'Pithampur',
        'Dewas', 'Mandideep', 'Sagar', 'Satna', 'Rewa', 'Katni',
        'Singrauli', 'Dhar', 'Ratlam', 'Khandwa', 'Betul', 'Chhindwara',
        'Neemuch', 'Mandsaur', 'Seoni', 'Bina'),
    'Rajasthan': (
        'Jaipur', 'Jodhpur', 'Udaipur', 'Kota', 'Ajmer', 'Bikaner',
        'Bhilwara', 'Alwar', 'Bhiwadi', 'Neemrana', 'Chittorgarh',
        'Barmer', 'Pali', 'Sikar', 'Jaisalmer', 'Banswara', 'Beawar'),
    'Uttar Pradesh': (
        'Lucknow', 'Kanpur', 'Noida', 'Greater Noida', 'Ghaziabad',
        'Agra', 'Varanasi', 'Prayagraj', 'Allahabad', 'Meerut',
        'Aligarh', 'Bareilly', 'Moradabad', 'Gorakhpur', 'Jhansi',
        'Mathura', 'Saharanpur', 'Firozabad', 'Unnao', 'Rae Bareli',
        'Sonbhadra', 'Renukoot', 'Obra', 'Anpara', 'Dadri'),
    'Uttarakhand': ('Dehradun', 'Haridwar', 'Roorkee', 'Rudrapur',
                    'Kashipur', 'Pantnagar', 'Haldwani', 'Sitarganj'),
    'Haryana': (
        'Gurugram', 'Gurgaon', 'Faridabad', 'Manesar', 'Panipat',
        'Sonipat', 'Ambala', 'Hisar', 'Karnal', 'Rohtak', 'Rewari',
        'Bawal', 'Yamunanagar', 'Bahadurgarh', 'Jhajjar', 'Dharuhera'),
    'Punjab': ('Ludhiana', 'Amritsar', 'Jalandhar', 'Patiala',
               'Bathinda', 'Mohali', 'Rajpura', 'Mandi Gobindgarh',
               'Pathankot', 'Phagwara'),
    'Himachal Pradesh': ('Shimla', 'Baddi', 'Solan', 'Nalagarh',
                         'Una', 'Paonta Sahib', 'Kullu', 'Mandi',
                         'Dharamshala', 'Parwanoo'),
    'Jammu and Kashmir': ('Srinagar', 'Jammu', 'Kathua', 'Samba',
                          'Udhampur', 'Ramban', 'Kishtwar'),
    'Ladakh': ('Leh', 'Kargil'),
    'Delhi': ('Delhi', 'New Delhi'),
    'Chandigarh': ('Chandigarh',),
    'Goa': ('Panaji', 'Panjim', 'Margao', 'Vasco', 'Vasco da Gama',
            'Mormugao', 'Verna', 'Ponda', 'Mapusa'),
    'Assam': ('Guwahati', 'Dibrugarh', 'Silchar', 'Jorhat', 'Tinsukia',
              'Numaligarh', 'Bongaigaon', 'Tezpur'),
    'Puducherry': ('Puducherry', 'Pondicherry', 'Karaikal'),
    'Dadra and Nagar Haveli and Daman and Diu': ('Silvassa', 'Daman',
                                                 'Diu'),
    'Andaman and Nicobar Islands': ('Port Blair', 'Great Nicobar'),
    'Sikkim': ('Gangtok',),
    'Meghalaya': ('Shillong', 'Byrnihat'),
    'Tripura': ('Agartala',),
    'Manipur': ('Imphal',),
    'Mizoram': ('Aizawl',),
    'Nagaland': ('Kohima', 'Dimapur'),
    'Arunachal Pradesh': ('Itanagar',),
}
CITY_STATE = {}
for _state, _cities in _CITIES.items():
    for _city in _cities:
        if _city.endswith((' MH', ' GJ', ' OD')):
            continue        # placeholders kept out of the lookup
        CITY_STATE[_city.lower()] = _state


def state_for_city(city):
    """The state a known city is in, or ''."""
    text = ' '.join(str(city or '').strip().lower().split()).strip('.,')
    if not text:
        return ''
    if text in CITY_STATE:
        return CITY_STATE[text]
    # "Mumbai - 400001", "Pune (MIDC Chakan)"
    head = re.split(r'[,\-(/0-9]', text, maxsplit=1)[0].strip()
    return CITY_STATE.get(head, '')


#: PIN code → state. Three-digit prefixes first, then two.
_PIN3 = {
    '160': 'Chandigarh', '194': 'Ladakh', '396': 'Gujarat',
    '403': 'Goa', '605': 'Puducherry', '737': 'Sikkim',
    '744': 'Andaman and Nicobar Islands', '682': 'Kerala',
    '790': 'Arunachal Pradesh', '791': 'Arunachal Pradesh',
    '792': 'Arunachal Pradesh', '793': 'Meghalaya', '794': 'Meghalaya',
    '795': 'Manipur', '796': 'Mizoram', '797': 'Nagaland',
    '798': 'Nagaland', '799': 'Tripura',
    '246': 'Uttarakhand', '247': 'Uttarakhand', '248': 'Uttarakhand',
    '249': 'Uttarakhand', '262': 'Uttarakhand', '263': 'Uttarakhand',
    '500': 'Telangana', '501': 'Telangana', '502': 'Telangana',
    '503': 'Telangana', '504': 'Telangana', '505': 'Telangana',
    '506': 'Telangana', '507': 'Telangana', '508': 'Telangana',
    '509': 'Telangana',
    '813': 'Jharkhand', '814': 'Jharkhand', '815': 'Jharkhand',
    '816': 'Jharkhand', '822': 'Jharkhand', '825': 'Jharkhand',
    '826': 'Jharkhand', '827': 'Jharkhand', '828': 'Jharkhand',
    '829': 'Jharkhand', '831': 'Jharkhand', '832': 'Jharkhand',
    '833': 'Jharkhand', '834': 'Jharkhand', '835': 'Jharkhand',
}
_PIN2 = {
    '11': 'Delhi', '12': 'Haryana', '13': 'Haryana', '14': 'Punjab',
    '15': 'Punjab', '16': 'Punjab', '17': 'Himachal Pradesh',
    '18': 'Jammu and Kashmir', '19': 'Jammu and Kashmir',
    '20': 'Uttar Pradesh', '21': 'Uttar Pradesh', '22': 'Uttar Pradesh',
    '23': 'Uttar Pradesh', '24': 'Uttar Pradesh', '25': 'Uttar Pradesh',
    '26': 'Uttar Pradesh', '27': 'Uttar Pradesh', '28': 'Uttar Pradesh',
    '30': 'Rajasthan', '31': 'Rajasthan', '32': 'Rajasthan',
    '33': 'Rajasthan', '34': 'Rajasthan', '36': 'Gujarat',
    '37': 'Gujarat', '38': 'Gujarat', '39': 'Gujarat',
    '40': 'Maharashtra', '41': 'Maharashtra', '42': 'Maharashtra',
    '43': 'Maharashtra', '44': 'Maharashtra', '45': 'Madhya Pradesh',
    '46': 'Madhya Pradesh', '47': 'Madhya Pradesh',
    '48': 'Madhya Pradesh', '49': 'Chhattisgarh',
    '51': 'Andhra Pradesh', '52': 'Andhra Pradesh',
    '53': 'Andhra Pradesh', '56': 'Karnataka', '57': 'Karnataka',
    '58': 'Karnataka', '59': 'Karnataka', '60': 'Tamil Nadu',
    '61': 'Tamil Nadu', '62': 'Tamil Nadu', '63': 'Tamil Nadu',
    '64': 'Tamil Nadu', '67': 'Kerala', '68': 'Kerala', '69': 'Kerala',
    '70': 'West Bengal', '71': 'West Bengal', '72': 'West Bengal',
    '73': 'West Bengal', '74': 'West Bengal', '75': 'Odisha',
    '76': 'Odisha', '77': 'Odisha', '78': 'Assam', '80': 'Bihar',
    '81': 'Bihar', '82': 'Bihar', '83': 'Bihar', '84': 'Bihar',
    '85': 'Bihar',
}

_PIN_RE = re.compile(r'(?<!\d)([1-8]\d{2})\s?(\d{3})(?!\d)')


def find_pin(text):
    """The first six-digit Indian PIN in a piece of text, or ''."""
    found = _PIN_RE.search(str(text or ''))
    return (found.group(1) + found.group(2)) if found else ''


def state_for_pin(pin):
    digits = re.sub(r'\D', '', str(pin or ''))
    if len(digits) != 6 or digits[0] in '09':
        return ''
    return _PIN3.get(digits[:3]) or _PIN2.get(digits[:2], '')
