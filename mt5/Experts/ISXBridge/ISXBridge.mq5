//+------------------------------------------------------------------+
//| ISXBridge.mq5                                                    |
//| Thin MT5 adapter for the Python close-confirmed ISX engine.       |
//| The default is shadow mode: no order is submitted.               |
//+------------------------------------------------------------------+
#property strict
#property version   "1.0"

#include <Trade\Trade.mqh>

input string InpBridgeUrl              = "http://127.0.0.1:8765/api/mt5/isx/decision";
input string InpBridgeToken            = "";
input int    InpPollSeconds            = 2;
input int    InpHistoryBars            = 300;
input int    InpTimeoutMs              = 1500;
input int    InpBrokerUtcOffsetHours   = 0;
input bool   InpShadowMode             = true;
input bool   InpEnableLiveExecution    = false;
input bool   InpAllowShort             = false;
input double InpLots                   = 0.01;
input double InpTargetR                = 4.0;
input double InpBreakEvenR             = 1.0;
input double InpProfitLockTriggerR     = 2.0;
input double InpProfitLockR            = 1.0;
input double InpMaxSpreadPoints        = 50.0;
input int    InpDeviationPoints        = 20;
input ulong  InpMagicNumber            = 26092501;

CTrade g_trade;
double g_last_setup_hash = 0.0;

//+------------------------------------------------------------------+
int OnInit()
  {
   if(InpPollSeconds < 1 || InpHistoryBars < 20 || InpTimeoutMs < 250 ||
      InpTargetR <= 0.0 || InpBreakEvenR <= 0.0 || InpProfitLockTriggerR <= 0.0 ||
      InpProfitLockR < 0.0 || InpLots <= 0.0)
      return(INIT_PARAMETERS_INCORRECT);

   g_trade.SetExpertMagicNumber(InpMagicNumber);
   g_trade.SetDeviationInPoints(InpDeviationPoints);
   g_last_setup_hash = LoadLastSetupHash();
   EventSetTimer(InpPollSeconds);
   PrintFormat("ISX bridge ready: %s shadow=%s live=%s", _Symbol,
               InpShadowMode ? "true" : "false",
               InpEnableLiveExecution ? "true" : "false");
   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   EventKillTimer();
  }

//+------------------------------------------------------------------+
void OnTimer()
  {
   ManagePositions();

   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick))
     {
      PrintFormat("[ISX STAND_DOWN] no tick for %s", _Symbol);
      return;
     }

   string payload = BuildPayload(tick);
   string response;
   int status = PostDecision(payload, response);
   if(status != 200)
     {
      PrintFormat("[ISX STAND_DOWN] bridge HTTP status=%d error=%d", status, GetLastError());
      return;
     }
   HandleDecision(response, tick);
  }

//+------------------------------------------------------------------+
string BuildPayload(const MqlTick &tick)
  {
   string session = LongToString(AccountInfoInteger(ACCOUNT_LOGIN)) + ":" + _Symbol;
   string body = "{\"protocol_version\":1,\"session_id\":\"" + JsonEscape(session) +
                 "\",\"symbol\":\"" + JsonEscape(_Symbol) + "\",\"target_r\":" +
                 DoubleToString(InpTargetR, 4) + ",\"bars\":{\"H4\":" +
                 BarsJson(PERIOD_H4) + ",\"H1\":" + BarsJson(PERIOD_H1) +
                 ",\"M15\":" + BarsJson(PERIOD_M15) + "},\"trigger\":{\"time\":" +
                 LongToString((long)TimeGMT()) + ",\"bid\":" + DoubleToString(tick.bid, _Digits) +
                 ",\"ask\":" + DoubleToString(tick.ask, _Digits) + "}}";
   return(body);
  }

//+------------------------------------------------------------------+
string BarsJson(const ENUM_TIMEFRAMES timeframe)
  {
   MqlRates rates[];
   ArraySetAsSeries(rates, false);
   int copied = CopyRates(_Symbol, timeframe, 1, InpHistoryBars, rates);
   if(copied <= 0)
      return("[]");

   string json = "[";
   for(int i = 0; i < copied; i++)
     {
      if(i > 0)
         json += ",";
      json += "{\"timestamp_utc\":\"" + IsoUtc(rates[i].time) + "\",\"open\":" +
              DoubleToString(rates[i].open, _Digits) + ",\"high\":" +
              DoubleToString(rates[i].high, _Digits) + ",\"low\":" +
              DoubleToString(rates[i].low, _Digits) + ",\"close\":" +
              DoubleToString(rates[i].close, _Digits) + ",\"complete\":true}";
     }
   return(json + "]");
  }

//+------------------------------------------------------------------+
int PostDecision(const string body, string &response)
  {
   char data[];
   char result[];
   string headers = "Content-Type: application/json\r\n";
   if(StringLen(InpBridgeToken) > 0)
      headers += "X-Jev-Bridge-Token: " + InpBridgeToken + "\r\n";
   StringToCharArray(body, data, 0, -1, CP_UTF8);
   ResetLastError();
   int status = WebRequest("POST", InpBridgeUrl, headers, InpTimeoutMs, data,
                           ArraySize(data) - 1, result, headers);
   if(status >= 0)
      response = CharArrayToString(result, 0, -1, CP_UTF8);
   return(status);
  }

//+------------------------------------------------------------------+
void HandleDecision(const string response, const MqlTick &tick)
  {
   string action = JsonString(response, "action");
   string phase = JsonString(response, "phase");
   string reason = JsonString(response, "reason");
   string setup = JsonString(response, "setup_id");
   string tag = JsonString(response, "execution_tag");
   bool allowed = JsonBool(response, "allowed");
   string side = JsonString(response, "side");
   double stop = JsonDouble(response, "stop", 0.0);

   PrintFormat("[ISX %s] phase=%s setup=%s reason=%s", action, phase, setup, reason);
   if(action != "ISX_X" || !allowed || StringLen(setup) == 0)
      return;

   double setup_hash = SetupHash(setup);
   if(setup_hash == g_last_setup_hash || HasOpenIsxPosition())
     {
      PrintFormat("[ISX STAND_DOWN] duplicate setup=%s", setup);
      return;
     }
   if(side == "SELL" && !InpAllowShort)
     {
      PrintFormat("[ISX STAND_DOWN] shorting disabled setup=%s", setup);
      return;
     }

   double entry = side == "BUY" ? tick.ask : tick.bid;
   double target = side == "BUY" ? entry + MathAbs(entry - stop) * InpTargetR
                                  : entry - MathAbs(entry - stop) * InpTargetR;
   if(!RiskAllows(side, entry, stop))
      return;

   string comment = "ISX|" + setup + "|X";
   if(InpShadowMode || !InpEnableLiveExecution)
     {
      PrintFormat("[ISX SHADOW] %s entry=%s stop=%s target=%s tag=%s", side,
                  DoubleToString(entry, _Digits), DoubleToString(stop, _Digits),
                  DoubleToString(target, _Digits), tag);
      g_last_setup_hash = setup_hash;
      SaveLastSetupHash(g_last_setup_hash);
      return;
     }

   bool sent = side == "BUY" ? g_trade.Buy(InpLots, _Symbol, 0.0, stop, target, comment)
                             : g_trade.Sell(InpLots, _Symbol, 0.0, stop, target, comment);
   if(!sent)
     {
      PrintFormat("[ISX REJECTED] retcode=%u %s", g_trade.ResultRetcode(), g_trade.ResultRetcodeDescription());
      return;
     }
   g_last_setup_hash = setup_hash;
   SaveLastSetupHash(g_last_setup_hash);
   PrintFormat("[ISX EXECUTED] %s setup=%s order=%I64u", side, setup, g_trade.ResultOrder());
   RememberPositionRisk(stop);
  }

//+------------------------------------------------------------------+
bool RiskAllows(const string side, const double entry, const double stop)
  {
   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick))
      return(false);
   double spread_points = (tick.ask - tick.bid) / _Point;
   if(spread_points > InpMaxSpreadPoints)
     {
      PrintFormat("[ISX STAND_DOWN] spread %.1f > %.1f points", spread_points, InpMaxSpreadPoints);
      return(false);
     }
   if((side == "BUY" && stop >= entry) || (side == "SELL" && stop <= entry) || stop <= 0.0)
     {
      Print("[ISX STAND_DOWN] invalid stop geometry");
      return(false);
     }
   long stops_level = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL);
   double minimum_distance = (double)stops_level * _Point;
   if(MathAbs(entry - stop) < minimum_distance)
     {
      PrintFormat("[ISX STAND_DOWN] stop distance %.1f points is below broker minimum", MathAbs(entry - stop) / _Point);
      return(false);
     }
   double volume_min = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double volume_max = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   if(InpLots < volume_min || InpLots > volume_max)
     {
      PrintFormat("[ISX STAND_DOWN] lots %.4f outside broker range %.4f..%.4f", InpLots, volume_min, volume_max);
      return(false);
     }
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED))
     {
      Print("[ISX STAND_DOWN] terminal trading is disabled");
      return(false);
     }
   return(true);
  }

//+------------------------------------------------------------------+
void ManagePositions()
  {
   if(InpShadowMode || !InpEnableLiveExecution)
      return;
   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick))
      return;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      string symbol = PositionGetSymbol(i);
      if(symbol != _Symbol || (ulong)PositionGetInteger(POSITION_MAGIC) != InpMagicNumber)
         continue;
      ulong ticket = (ulong)PositionGetInteger(POSITION_TICKET);
      long type = PositionGetInteger(POSITION_TYPE);
      double open = PositionGetDouble(POSITION_PRICE_OPEN);
      double stop = PositionGetDouble(POSITION_SL);
      double target = PositionGetDouble(POSITION_TP);
      double initial_risk = LoadPositionRisk(ticket, MathAbs(open - stop));
      if(initial_risk <= 0.0)
         continue;
      double favorable = type == POSITION_TYPE_BUY ? (tick.bid - open) / initial_risk
                                                   : (open - tick.ask) / initial_risk;
      double desired = stop;
      if(favorable >= InpBreakEvenR)
         desired = open;
      if(favorable >= InpProfitLockTriggerR)
         desired = type == POSITION_TYPE_BUY ? open + InpProfitLockR * initial_risk
                                             : open - InpProfitLockR * initial_risk;
      if(!ImprovesStop(type, stop, desired))
         continue;
      if(g_trade.PositionModify(_Symbol, NormalizeDouble(desired, _Digits), target))
         PrintFormat("[ISX MANAGEMENT] %s stop moved to %s at %.2fR", _Symbol,
                     DoubleToString(desired, _Digits), favorable);
     }
  }

//+------------------------------------------------------------------+
bool ImprovesStop(const long type, const double current, const double desired)
  {
   if(desired <= 0.0)
      return(false);
   if(type == POSITION_TYPE_BUY)
      return(current <= 0.0 || desired > current + _Point / 2.0);
   return(current <= 0.0 || desired < current - _Point / 2.0);
  }

//+------------------------------------------------------------------+
bool HasOpenIsxPosition()
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      string symbol = PositionGetSymbol(i);
      if(symbol == _Symbol && (ulong)PositionGetInteger(POSITION_MAGIC) == InpMagicNumber)
         return(true);
     }
   return(false);
  }

//+------------------------------------------------------------------+
void RememberPositionRisk(const double stop)
  {
   if(!PositionSelect(_Symbol))
      return;
   if((ulong)PositionGetInteger(POSITION_MAGIC) != InpMagicNumber)
      return;
   ulong ticket = (ulong)PositionGetInteger(POSITION_TICKET);
   double risk = MathAbs(PositionGetDouble(POSITION_PRICE_OPEN) - stop);
   if(risk > 0.0)
      GlobalVariableSet(RiskKey(ticket), risk);
  }

//+------------------------------------------------------------------+
double LoadPositionRisk(const ulong ticket, const double fallback)
  {
   string key = RiskKey(ticket);
   if(GlobalVariableCheck(key))
      return(GlobalVariableGet(key));
   if(fallback > 0.0)
      GlobalVariableSet(key, fallback);
   return(fallback);
  }

//+------------------------------------------------------------------+
string RiskKey(const ulong ticket)
  {
   return("ISX_RISK_" + LongToString((long)ticket));
  }

//+------------------------------------------------------------------+
double LoadLastSetupHash()
  {
   string key = "ISX_SETUP_" + _Symbol;
   return(GlobalVariableCheck(key) ? GlobalVariableGet(key) : 0.0);
  }

//+------------------------------------------------------------------+
void SaveLastSetupHash(const double value)
  {
   GlobalVariableSet("ISX_SETUP_" + _Symbol, value);
  }

//+------------------------------------------------------------------+
double SetupHash(const string value)
  {
   ulong hash = 2166136261;
   for(int i = 0; i < StringLen(value); i++)
      hash = (hash ^ (ulong)StringGetCharacter(value, i)) * 16777619;
   return((double)hash);
  }

//+------------------------------------------------------------------+
string IsoUtc(const datetime server_time)
  {
   MqlDateTime dt;
   TimeToStruct(server_time - InpBrokerUtcOffsetHours * 3600, dt);
   return(StringFormat("%04d-%02d-%02dT%02d:%02d:%02dZ", dt.year, dt.mon, dt.day,
                       dt.hour, dt.min, dt.sec));
  }

//+------------------------------------------------------------------+
string JsonEscape(string value)
  {
   StringReplace(value, "\\", "\\\\");
   StringReplace(value, "\"", "\\\"");
   return(value);
  }

//+------------------------------------------------------------------+
string JsonString(const string json, const string key)
  {
   string needle = "\"" + key + "\":\"";
   int start = StringFind(json, needle);
   if(start < 0)
      return("");
   start += StringLen(needle);
   int end = StringFind(json, "\"", start);
   return(end < 0 ? "" : StringSubstr(json, start, end - start));
  }

//+------------------------------------------------------------------+
bool JsonBool(const string json, const string key)
  {
   string needle = "\"" + key + "\":";
   int start = StringFind(json, needle);
   if(start < 0)
      return(false);
   start += StringLen(needle);
   return(StringSubstr(json, start, 4) == "true");
  }

//+------------------------------------------------------------------+
double JsonDouble(const string json, const string key, const double fallback)
  {
   string needle = "\"" + key + "\":";
   int start = StringFind(json, needle);
   if(start < 0)
      return(fallback);
   start += StringLen(needle);
   int end = start;
   while(end < StringLen(json) && StringFind("0123456789+-.eE", StringSubstr(json, end, 1)) >= 0)
      end++;
   string value = StringSubstr(json, start, end - start);
   return(StringToDouble(value));
  }
//+------------------------------------------------------------------+
