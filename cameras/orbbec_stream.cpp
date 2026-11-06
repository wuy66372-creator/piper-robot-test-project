// Continuous color transport for Show-Harness, using the same pinned SDK as RGB-D capture.
#include <libobsensor/ObSensor.hpp>
#include <atomic>
#include <chrono>
#include <csignal>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <memory>
#include <vector>
#include <deque>
#include <mutex>
#include <thread>
std::atomic<bool> running{true};
void stop(int){running=false;}
double monotonic(){return std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch()).count();}
int main(int argc,char**argv){
 if(argc!=2)return 2;
 std::signal(SIGTERM,stop);std::signal(SIGINT,stop);
 try{
  std::filesystem::path dir(argv[1]);std::filesystem::create_directories(dir);
  ob::Context::setLoggerSeverity(OB_LOG_SEVERITY_ERROR);ob::Context ctx;
  auto devices=ctx.queryDeviceList();if(devices->deviceCount()!=2)throw std::runtime_error("expected both registered wrist cameras");
  std::vector<std::shared_ptr<ob::Pipeline>> pipelines;
  for(uint32_t i=0;i<2;i++){
   auto dev=devices->getDevice(i);std::string serial=dev->getDeviceInfo()->serialNumber();
   if(serial!="CC1365301HT"&&serial!="CC1365301R4")throw std::runtime_error("unexpected camera serial");
   auto pipe=std::make_shared<ob::Pipeline>(dev);auto conf=std::make_shared<ob::Config>();
   conf->enableStream(pipe->getStreamProfileList(OB_SENSOR_COLOR)->getVideoStreamProfile(640,480,OB_FORMAT_MJPG,15));
   auto history=std::make_shared<std::deque<std::string>>();
   auto lock=std::make_shared<std::mutex>();
   pipe->start(conf,[dir,serial,history,lock](std::shared_ptr<ob::FrameSet> frames){
    std::lock_guard<std::mutex> guard(*lock);
    auto c=frames->colorFrame();if(!c||c->format()!=OB_FORMAT_MJPG)return;
    double arrived=monotonic();auto name=serial+"_"+std::to_string(c->index())+".jpg";
    auto tmp=dir/(serial+".partial.jpg");std::ofstream jpg(tmp,std::ios::binary);
    jpg.write((const char*)c->data(),c->dataSize());jpg.close();if(!jpg)return;
    std::filesystem::rename(tmp,dir/name);
    std::ofstream meta(dir/(serial+".partial.json"));
    meta<<std::setprecision(17)<<"{\"file\":"<<std::quoted(name)<<",\"arrival_monotonic_s\":"<<arrived<<",\"index\":"<<c->index()<<"}\n";
    meta.close();if(!meta)return;std::filesystem::rename(dir/(serial+".partial.json"),dir/(serial+".json"));
    history->push_back(name);
    if(history->size()>20){std::filesystem::remove(dir/history->front());history->pop_front();}
   });pipelines.push_back(pipe);
  }
  while(running)std::this_thread::sleep_for(std::chrono::milliseconds(50));
  for(auto&p:pipelines)p->stop();return 0;
 }catch(ob::Error&e){std::cerr<<e.getMessage()<<std::endl;return 1;}
 catch(std::exception&e){std::cerr<<e.what()<<std::endl;return 1;}
}
