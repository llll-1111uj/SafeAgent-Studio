import urllib.request,socket,io,sys,json
sys.stdout=io.TextIOWrapper(sys.stdout.buffer,encoding="utf-8",errors="replace")
socket.setdefaulttimeout(15)
hdr={"Referer":"http://www.weather.com.cn/","User-Agent":"Mozilla/5.0"}
d=urllib.request.urlopen(urllib.request.Request("http://d1.weather.com.cn/sk_2d/101060101.html",headers=hdr)).read().decode("utf-8","ignore")
j=json.loads(d[d.index(chr(123)):])
print("CITY="+j["cityname"])
print("DATE="+j["date"]+" "+j["time"])
print("TEMP="+j["temp"]+"C HUM="+j["SD"]+" PRES="+j["qy"]+"hPa")
print("WIND="+j["WD"]+" "+j["WS"]+" AQI="+j["aqi"]+" VIS="+j["njd"])
print("WEATHER="+j["weather"]+" / "+j["weathere"])