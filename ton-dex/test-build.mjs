// Offline SDK compatibility test: builds messages with a provider that forbids RPC.
import {readFileSync} from 'node:fs';
import {Address,openContract} from '@ton/core';
import {dexFactory} from '@ston-fi/sdk';
const pins=JSON.parse(readFileSync(new URL('./routers.json',import.meta.url),'utf8'));
const pin=pins.find(r=>r.major_version===2&&r.minor_version===1&&r.router_type==='ConstantProduct')??pins[0];
const metadata={address:pin.address,majorVersion:pin.major_version,minorVersion:pin.minor_version,ptonMasterAddress:pin.pton_master_address,ptonVersion:pin.pton_version,ptonWalletAddress:pin.pton_wallet_address,routerType:pin.router_type};
const contracts=dexFactory(metadata),owner=new Address(0,Buffer.alloc(32,0x11)).toString(),offerWallet=new Address(0,Buffer.alloc(32,0x44)).toString(),askWallet=new Address(0,Buffer.alloc(32,0x55)).toString();
const provider={get:async()=>{throw Error('UNEXPECTED_RPC')},open:contract=>openContract(contract,()=>provider)};
const router=openContract(contracts.Router.create(pin.address),()=>provider),proxyTon=contracts.pTON.create(pin.pton_master_address);
const deadline=Math.floor(Date.now()/1000)+120,queryId=(1n<<62n)+42n;
const common={userWalletAddress:owner,receiverAddress:owner,refundAddress:owner,excessesAddress:owner,queryId,minAskAmount:'100000',deadline,proxyTon,referralValue:0};
const usdt=await router.getSwapJettonToTonTxParams({...common,offerAmount:'1000000',offerJettonAddress:'EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs',offerJettonWalletAddress:offerWallet,askJettonWalletAddress:pin.pton_wallet_address});
const ton=await router.getSwapTonToJettonTxParams({...common,offerAmount:'1000000000',askJettonAddress:'EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs',offerJettonWalletAddress:pin.pton_wallet_address,askJettonWalletAddress:askWallet});
const serialize=t=>({to:t.to.toString(),value:t.value.toString(),body:t.body.toBoc().toString('base64')});
console.log(JSON.stringify({owner,offerWallet,router:metadata,query_id:queryId.toString(),deadline,USDT:serialize(usdt),TON:serialize(ton)}));
