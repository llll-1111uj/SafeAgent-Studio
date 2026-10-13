import urllib.request,socket,io,sys,json
sys.stdout=io.TextIOWrapper(sys.stdout.buffer,encoding="utf-8",errors="replace")
socket.setdefaulttimeout(15)
hdr={"Referer":"http://www.weather.com.cn/","User-Agent":"Mozilla/5.0"}
d=urllib.request.urlopen(urllib.request.Request("http://d1.weather.com.cn/sk_2d/101060101.html",headers=hdr)).read().decode("utf-8","ignore")
j=json.loads(d[d.index("="):].lstrip("="))
print(j["cityname"],"实况",j["date"],j["time"])
print("温度",j["temp"],"C, 湿度",j["SD"],"气压",j["qy"],"hPa, 风",j["WD"],j["WS"],"AQI",j["aqi"],"天气",j["weather"],"能见度",j["njd"])