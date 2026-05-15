import re

s = input("Full DMS Coordinates: \n")
parts = re.findall(r"[\d.]+|[NSEW]", s)

def convert(d, m, s, dir):
    res = float(d) + float(m)/60 + float(s)/3600
    return -res if dir in 'SW' else res

lat, lon = convert(*parts[:4]), convert(*parts[4:])

print(f"Lat: {lat}\nLon: {lon}")