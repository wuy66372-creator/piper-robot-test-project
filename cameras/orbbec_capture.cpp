#include <libobsensor/ObSensor.hpp>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <memory>
#include <mutex>
#include <thread>
#include <vector>
struct State {
 std::shared_ptr<ob::Pipeline> pipe;
 std::string serial,uid,firmware;
 std::mutex mutex;
 std::shared_ptr<ob::DepthFrame> depth;
 std::shared_ptr<ob::ColorFrame> color;
 uint64_t depthCount=0,colorCount=0,depthFirst=0,colorFirst=0,depthLast=0,colorLast=0;
 double depthArrival=0,colorArrival=0,depthFirstArrival=0,colorFirstArrival=0;
};
double steady(){return std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch()).count();}
void raw(const std::string&p,const void*d,size_t n){std::ofstream f(p,std::ios::binary);f.write((const char*)d,n);if(!f)throw std::runtime_error("write failed: "+p);}
void intrinsic(std::ostream&o,const OBCameraIntrinsic&i){o<<"{\"fx\":"<<i.fx<<",\"fy\":"<<i.fy<<",\"cx\":"<<i.cx<<",\"cy\":"<<i.cy<<",\"width\":"<<i.width<<",\"height\":"<<i.height<<"}";}
int main(int argc,char**argv){
 if(argc!=2){std::cerr<<"Usage: orbbec_capture OUTPUT_DIRECTORY\n";return 2;}
 try{
  ob::Context::setLoggerSeverity(OB_LOG_SEVERITY_ERROR);ob::Context ctx;
  auto devices=ctx.queryDeviceList();if(devices->deviceCount()!=2)throw std::runtime_error("expected two cameras");
  std::filesystem::path dest(argv[1]);
  if(std::filesystem::exists(dest)&&!std::filesystem::is_empty(dest))throw std::runtime_error("output directory must be empty");
  std::filesystem::create_directories(dest);
  std::vector<std::shared_ptr<State>> states;
  for(uint32_t i=0;i<2;i++){
   auto state=std::make_shared<State>();auto dev=devices->getDevice(i);auto info=dev->getDeviceInfo();
   state->serial=info->serialNumber();state->uid=info->uid();state->firmware=info->firmwareVersion();
   if(state->serial!="CC1365301HT"&&state->serial!="CC1365301R4")throw std::runtime_error("unregistered camera");
   state->pipe=std::make_shared<ob::Pipeline>(dev);auto conf=std::make_shared<ob::Config>();
   conf->enableStream(state->pipe->getStreamProfileList(OB_SENSOR_DEPTH)->getVideoStreamProfile(640,400,OB_FORMAT_Y11,15));
   conf->enableStream(state->pipe->getStreamProfileList(OB_SENSOR_COLOR)->getVideoStreamProfile(640,480,OB_FORMAT_MJPG,15));
   conf->setFrameAggregateOutputMode(OB_FRAME_AGGREGATE_OUTPUT_ANY_SITUATION);
   state->pipe->start(conf,[weak=std::weak_ptr<State>(state)](std::shared_ptr<ob::FrameSet> frames){
    auto state=weak.lock();if(!state)return;
    std::lock_guard<std::mutex> g(state->mutex);auto d=frames->depthFrame();auto c=frames->colorFrame();
    if(d&&(!state->depth||d->index()!=state->depth->index())){if(!state->depthCount){state->depthFirst=d->timeStampUs();state->depthFirstArrival=steady();}state->depthCount++;state->depthLast=d->timeStampUs();state->depth=d;state->depthArrival=steady();}
    if(c&&(!state->color||c->index()!=state->color->index())){if(!state->colorCount){state->colorFirst=c->timeStampUs();state->colorFirstArrival=steady();}state->colorCount++;state->colorLast=c->timeStampUs();state->color=c;state->colorArrival=steady();}
   });
   states.push_back(state);
  }
  std::this_thread::sleep_for(std::chrono::seconds(10));
  std::ofstream meta(dest/"capture.partial.json");meta<<"{\"sdk\":\"1.10.37\",\"simultaneous_cameras\":2,\"aligned\":false,\"hardware_synchronized\":false,\"device_timestamp_clock_verified\":false,\"cameras\":[";
  bool first=true;
  for(auto&s:states){
   if(!first)meta<<",";first=false;std::lock_guard<std::mutex>g(s->mutex);
   if(!s->depth||!s->color||s->depthCount<20||s->colorCount<20)throw std::runtime_error("missing camera streams");
   auto d=s->depth;auto c=s->color;double now=steady();
   if(now-s->depthArrival>.5||now-s->colorArrival>.5)throw std::runtime_error("stale camera streams");
   if(d->dataSize()!=d->width()*d->height()*2||c->format()!=OB_FORMAT_MJPG)throw std::runtime_error("unexpected frame layout");
   raw((dest/(s->serial+"_depth_u16le.bin")).string(),d->data(),d->dataSize());
   raw((dest/(s->serial+"_color.jpg")).string(),c->data(),c->dataSize());
   auto p=s->pipe->getCameraParam();
   meta<<"{\"serial\":"<<std::quoted(s->serial)<<",\"uid\":"<<std::quoted(s->uid)<<",\"firmware\":"<<std::quoted(s->firmware)
       <<",\"depth\":{\"width\":"<<d->width()<<",\"height\":"<<d->height()<<",\"format_enum\":"<<d->format()<<",\"scale_mm\":"<<d->getValueScale()
       <<",\"frames\":"<<s->depthCount<<",\"first_device_timestamp_us\":"<<s->depthFirst<<",\"last_device_timestamp_us\":"<<s->depthLast
       <<",\"host_system_timestamp_us\":"<<d->systemTimeStampUs()<<",\"observed_host_fps\":"<<(s->depthCount-1)/(s->depthArrival-s->depthFirstArrival)<<",\"last_arrival_monotonic_s\":"<<std::setprecision(15)<<s->depthArrival<<",\"last_arrival_age_ms\":"<<(now-s->depthArrival)*1000<<"}"
       <<",\"color\":{\"width\":"<<c->width()<<",\"height\":"<<c->height()<<",\"format\":\"MJPG\",\"frames\":"<<s->colorCount
       <<",\"first_device_timestamp_us\":"<<s->colorFirst<<",\"last_device_timestamp_us\":"<<s->colorLast<<",\"host_system_timestamp_us\":"<<c->systemTimeStampUs()
       <<",\"observed_host_fps\":"<<(s->colorCount-1)/(s->colorArrival-s->colorFirstArrival)<<",\"last_arrival_monotonic_s\":"<<std::setprecision(15)<<s->colorArrival<<",\"last_arrival_age_ms\":"<<(now-s->colorArrival)*1000<<"},\"depth_intrinsic\":";intrinsic(meta,p.depthIntrinsic);
   meta<<",\"color_intrinsic\":";intrinsic(meta,p.rgbIntrinsic);meta<<"}";
  }
   meta<<"]}\n";meta.close();if(!meta)throw std::runtime_error("metadata write failed");
   std::filesystem::rename(dest/"capture.partial.json",dest/"capture.json");for(auto&s:states)s->pipe->stop();
  std::cout<<"capture complete: "<<(dest/"capture.json").string()<<std::endl;return 0;
 }catch(ob::Error&e){std::cerr<<"Orbbec: "<<e.getMessage()<<std::endl;return 1;}catch(std::exception&e){std::cerr<<e.what()<<std::endl;return 1;}
}
