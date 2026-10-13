import urllib.request, socket
socket.setdefaulttimeout(12)
hdr={"Referer":"http://www.weather.com.cn/","User-Agent":"Mozilla/5.0"}
urls=["http://d1.weather.com.cn/sk_2d/101060101.html","http://d1.weather.com.cn/weather_index/101060101.html"]
for u in urls:
    try:
        d=urllib.request.urlopen(urllib.request.Request(u,headers=hdr)).read().decode("utf-8","ignore")
        print("OK",u,len(d));print(d[:700])
    except Exception as e:
        print("ERR",u,repr(e))