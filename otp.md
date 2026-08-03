---
tags:
  - encryption
  - otp
---

todo:

1. **Sanitization doesn't handle digits**, and real field messages are full of them (times, grid references, counts). Pick a convention now — spell digits out, or define a figure-shift marker — and state it next to the å note.
2. **For the Dice stub, the pitfall to pre-empt is modulo bias.** Six doesn't divide 26, so naive d6-to-letter mappings favor some letters — which would contradict your own bias section. The standard fix is rejection sampling: e.g., two dice give 36 ordered outcomes, map 26 of them, re-roll the other 10.
3. **Minor nits:** Caesar's cipher historically means the specific shift-3 (the general family is "shift cipher", ROTn the naming scheme) — your framing works, just phrase it so a pedant can't derail readers. And the one-way channel convention deserves its "why": if both parties sent from the same pad, they could encrypt with the same page simultaneously — an accidental two-time pad. Stating the reason makes the rule stick.
4. **Optional enrichment:** VENONA is the canonical cautionary tale for the reuse rule — wartime production pressure led the Soviets to duplicate pad pages, and that alone let the US decrypt years of traffic. One sentence of history sells the rule better than any math.
# Introduction

One time pad is a theoretically unbreakable encryption method that requires sender and receiver to possess the same identical key-data. It is ideal for field use where humans will be manually encrypting/decrypting messages. A sender / receiver pair is called a 'channel', and is practically speaking one-way, so that if you need two-way comms then you'll have two channels or two pairs between the sender and receiver.

The name is quite descriptive of how the scheme works. In a single channel you have two pads, A and B, which are identical. Each pad has a certain number of pages, and you use at least one page per message that you send / receive. After a page has been used to either encrypt an outgoing, or decrypt an incoming message, the page is destroyed and must absolutely never be used again.

Anyone may capture the encrypted message in transmission, but they can not decrypt it without the correct key-data (written on the page). If the page is correctly destroyed on both ends after use, then no one can ever decrypt the message again. It has simply become random noise at that point.

In order to understand OTP it is helpful to first understand Caesar's Cipher. Caesar's Cipher can then be understood as a terrible implementation of OTP, where the key is a single character repeating indefinitely. Or alternatively, OTP is Caesar's Cipher where each character has its own unique rotation, as determined by the key. More on that soon.

# Example transmission

I want to perform a basic encrypt/decrypt cycle so that we have something concrete to discuss further. Below is a tiny example page that we'll be using. Note that the spaces on this page are only there to make it readable, they are not part of the key.

```
FEKXE TQSIF CBIAH NZENE UURWQ 
```

Now we'll write a secret message: 
```
Activate Nåughtfire.
```

Before we can apply our encryption we have to sanitize the message by removing spaces, punctuation, case, and substitute weird Scandinavian letters. How we're going to substitute for å is just a matter of convention, I will simply replace with 'a', but you could replace with two a's for example, or 'au'. That is not part of the encryption scheme.

```
ACTIVATENAUGHTFIRE
```

Then we pad the message so that it matches the length of the key. The key length is: 5 x 5 = 25 characters.

```
ACTIVATENAUGHTFIREXXXXXXX
```

Padding the message is not a strict requirement of OTP, but I prefer to do it because it prevents metadata leakage. If we don't pad, an adversary will know the exact length of the message being sent. 
## Encryption

Now we may begin the encryption process. First of all we have to convert both the key and the message into numbers, where A = 0, B =1, and so forth:

Index (just for convenience)
```
A 00
B 01
C 02
D 03
E 04
F 05
G 06
H 07
I 08
J 09
K 10
L 11
M 12
N 13
O 14
P 15
Q 16
R 17
S 18
T 19
U 20
V 21
W 22
X 23
Y 24
Z 25
```

Key - converted to numbers
```
05 04 10 23 04 19 16 18 08 05 02 01 08 00 07 13 25 04 13 04 20 20 17 22 16 
```

Message - converted to numbers

```
00 02 19 08 21 00 19 04 13 00 20 06 07 19 05 08 17 04 23 23 23 23 23 23 23
```

We still haven't encrypted anything, we have only converted our key and our message into a format that we can do math with. Each digit pair represents a letter of the alphabet, 0-25. The encrypted output must also be in that format. 

At this point it's helpful to think of Caesar's Cipher again. Caesar's cipher basically uses a single number 1-25 to rotate each character. It's also called ROT cipher, or ROT3 for example if the key is 3, ROT4 if 4 and so forth. We're going to do the same thing here, except each character gets its own ROTn, where n is determined by the corresponding character from the key.

The first letter of the message, A (or 00) should be "rotated" by the first letter of the key; F or 05. Doing that is easy, we just add them together and that gives us 05 (F) The second letter is also simple, we add 04 to 02 and get 06 (G). But in the third letter we need to do something, because 10 and 19 gives us 29, which isn't a valid letter.

This is where the concept of 'rotating' makes sense. Once we are at the last letter in the alphabet, Z or 25, the next one has to start from A or 00 again. We can count on our fingers to figure that out, we go to 25/Z, still got four to go, so that's A, B, C, D or 03. There is a way to express this mathematically, using a special operator called 'modulo'. 

## Modulo Operation

Think of modulo as kind of like division, except that you only care about the remainder. 

```
10 / 2 = 5 (this is normal division)
10 mod 2 = 0 (because there is no remainder left from 10 / 2)

11 / 2 = 5 + remainder
11 mod 2 = 1 (because 11 / 2 has a remainder of 1)

12 / 2 = 6
12 mod 2 = 0
```

It can be a bit confusing, but it's mostly a matter of being difficult to explain. It is actually quite intuitive when you experience how it behaves in practical situations. 

Back to our third character, which adds up to 29, we can just do a modulo operation against the size of our alphabet and we'll end up with the correct final number:

```
29 mod 26 = 3
```

If you don't have a calculator or computer at hand, you'll probably still rotate the characters with your fingers so to speak, or in your head, but I think it's still useful to know that the modulo operator does exactly this.

## Final Ciphertext

So now we can apply the full key to the message and get our cipher. We don't have to decide if a character needs the modulo operator or not, it won't have any effect unless the addition exceeds the size of the alphabet. So we can simply do 
```
(Mn + Kn) mod 26
```
*Where Mn is message character N, and Kn is key character N.*

```
00 02 19 08 21 00 19 04 13 00 20 06 07 19 05 08 17 04 23 23 23 23 23 23 23
05 04 10 23 04 19 16 18 08 05 02 01 08 00 07 13 25 04 13 04 20 20 17 22 16 

05 06 03 05 25 19 09 22 21 05 22 07 15 19 12 21 16 08 10 01 17 17 14 19 13
```

Now we convert it back to letters and group them in fives:

```
FGDFZ TJWVF WHPTM VQIKB RROTN
```

It's a somewhat tedious process to do by hand, but with some practice it goes pretty fast, and as far as encryption goes it really doesn't get much simpler than this. 

## Decryption

Now that we have our final cipher, the message is encrypted and ready to be broadcast to the world. No one can decrypt our message except whoever has the same pad page that we used to encrypt it with.

The operation to decrypt is simply the inverse, instead of adding the key to the message we subtract the key, and use modulo to ensure that we don't land on negative numbers. It's the same principle as earlier when the addition results in an number higher than 25, we have to "roll-over" to 0 (A) again. In this case, if the subtraction results in a number lower than 0, we have to "roll-over" to 25 (Z) again.
```
(Cn - Kn) mod 26
```
*Where Cn is the ciphertext character N, and Kn is key character N.*

*NOTE: if doing this on a calculator, use `((Cn - Kn) + 26) mod 26` to avoid negative results*

```
05 06 03 05 25 19 09 22 21 05 22 07 15 19 12 21 16 08 10 01 17 17 14 19 13
05 04 10 23 04 19 16 18 08 05 02 01 08 00 07 13 25 04 13 04 20 20 17 22 16 

00 02 19 08 21 00 19 04 13 00 20 06 07 19 05 08 17 04 23 23 23 23 23 23 23
```

We can already see that this is identical to the original message, so converted back to letters this gives:

```
ACTIVATENAUGHTFIREXXXXXXX
```

The X's are clearly padding, and we can add spaces

```
ACTIVATE NAUGHTFIRE
```

## De-briefing

In our example we used a pretty small page size for convenience, but if the page was say three-hundred characters long rather than twenty-five then we would need a lot of padding at the end of our short message. It would be extremely tedious to perform modulo operations on hundreds of padding letters.

Fortunately we can be smarter about the padding, instead of padding with X we can simply pad with A (00), and then we don't need to do any math at all on it. Instead, from where the padding begins we can simply copy the key character-by-character. Let me show you:

```
ACTIVATENAUGHTFIREAAAAAAA
```

then becomes

```
00 02 19 08 21 00 19 04 13 00 20 06 07 19 05 08 17 04   00 00 00 00 00 00 00
```

and just for reference our key was

```
05 04 10 23 04 19 16 18 08 05 02 01 08 00 07 13 25 04   13 04 20 20 17 22 16 
```

The part that contains our actual message remains the same as before

```
05 06 03 05 25 19 09 22 21 05 22 07 15 19 12 21 16 08   13 04 20 20 17 22 16 
```

And the padding just repeats the key from the same position. 

The ciphertext in the padding region is raw key, and unused truly-random key is uniformly distributed — exactly as uniform as (message + key). So a passive observer can't tell where message ends and padding begins even if the convention is public, and length stays hidden.

# Critical technicalities

There are a few details that are absolutely essential to be aware of for secure OTP communication. Never compromise on these points, they are fundamental to the OTP scheme itself. Be aware that following these requirements is not in itself a guarantee against compromise. 

#### **The key data used must be at least as long as the message** 
If your message is longer than a single page-key, then you need to use multiple pages. If the key is shorter than the message, then you are not using OTP, but a **Vigenère cipher**. The moment the key is shorter than the plaintext and gets reused/repeated, you lose the information-theoretic security of OTP and become vulnerable to Kasiski examination and frequency analysis.

#### **A page must never be re-used**
If you subtract two ciphertexts that used the same key, the key cancels out and you are left with the difference of the two plaintexts:

```
C1 - C2  =  (P1 + K) - (P2 + K)  =  P1 - P2   (mod 26)
```

From there, an attacker can use **crib dragging**: guessing common words or phrases, subtracting them from `P1 - P2` at various offsets, and checking whether the result at that position looks like plausible text. Each correct guess reveals part of _both_ plaintexts simultaneously, which gives you more cribs to work with, and it cascades from there.

#### **The key data (that lives on the pages) must be truly random**
If the key comes from a PRNG, the entire keystream is determined by a small seed. An attacker only needs to crack the seed to recover everything, your OTP has silently become a stream cipher.

If the key comes from a biased source (e.g. a hardware RNG that favors certain letters), that bias passes straight through the addition into the ciphertext, leaking information about the plaintext. True randomness is what makes OTP information-theoretically secure; anything less reduces it to computationally-dependent security at best.

#### **The pads must remain secret from generation to destruction**
Everything rests on a single assumption: only the two endpoints possess the key material. There is no password and no hard math protecting you — physical possession of a page *is* the ability to decrypt. Assume the adversary knows exactly how OTP works and has recorded every transmission you've ever made; the pads are the only secret in the entire system.

That secrecy must hold across the pad's whole life-cycle: generation, storage, handover, use, destruction. Every stage is an exposure window, and a compromise at any of them breaks every message the page ever touches — including messages already sent, because an adversary who records ciphertext can simply wait for the key to leak. The earlier claim that a transmitted message "becomes random noise forever" is only true if custody never failed.

Compromise is also silent. A pad does not need to be stolen to be lost — a photograph of the pages is just as fatal, and it leaves no trace at all. The rule must therefore be absolute: if a pad has ever been outside your control, even briefly, treat it and everything encrypted with it as compromised, and retire it.

Two consequences follow. First, key material can only be exchanged by physically secure means: handed over in person, or by a courier you would trust with the plaintext itself. You cannot bootstrap OTP over an insecure channel — the pad's real function is to move the secure meeting in time, exchanging keys face-to-face today to protect messages you haven't written yet. This is the structural price of OTP, and there is no way around it. Second, destruction must be genuine and verified at both ends: a page that can be reassembled, un-deleted, or read back out of a printer's memory was never destroyed. Burn paper to ash, and treat any computer or printer that ever touched key material as part of the pad.

#### **A message must never be trusted without authentication**
OTP gives you perfect secrecy, but it gives you *zero* integrity. The scheme is fully malleable: because every ciphertext character is just `Pn + Kn`, anyone who can touch the message in transit can shift any plaintext character by any amount, without knowing the key:

```
Cn + d  =  (Pn + Kn) + d  =  (Pn + d) + Kn   (mod 26)
```

The receiver decrypts as normal and gets `Pn + d`. Nothing looks wrong.

An attacker who knows (or guesses) the plaintext at some position can therefore rewrite it to anything they want by adding the right difference. Field messages are full of guessable structure — callsigns, stereotyped phrases, padding conventions — and every guessed character is an editable character. Turning DAWN into DUSK takes four small additions:

```
D A W N     03 00 22 13
D U S K     03 20 18 10
deltas      00 20 22 23   ← added to the ciphertext, no key needed
```

Even an attacker who knows nothing about the content can flip characters blindly, and the receiver can not distinguish tampering from ordinary transmission garble.

Encryption is not authentication. Never act on a message that has not been authenticated, and never derive the authentication from the same key characters that encrypted the message — an appended checksum gets edited just as easily as the text it protects. See the Authentication section for how to do this properly.

# Preparation

## Random Key Generation
The most practical way to generate key-pages is by using computers or specialized electronics, since you need a lot of random data. But it can become relevant to generate OTP keys without access to electronics. 

Arguably the most important thing to be aware of when generating randomness is that we can not rely on human sourced data. For example asking humans to say a "random" letter will never be random, choosing "random" characters from a book is also never random. It is essential that humans, and any human constructs are viewed as sources of bias. Even our human sense of what constitutes random is inherently flawed, so that something *feels* random should be given no credence.

![[human_bias.png|697]]
**DO NOT UNDERESTIMATE THE PREDICTABILITY OF HUMANS!** 

There are endlessly many ways to generate random data, so I will provide a few examples here for inspiration purposes. The main challenge that all these methods will have in common is to eliminate human bias. Our movements are not random, even if the intention behind the movements is to be random.

### Dice
### Shake & Draw


## Operative Training


# Communication Protocols
Pads has to be handed over, messages has to be broadcast, received, and the fact that there is a message to receive at all has to be communicated. 

## Key/Pad Handover

# Security & Integrity
Human error is the most likely path of failure. 
## Protecting Our Network
## Protecting Our Cell
## Protecting Ourselves

## Authentication

## Tools
Tabula Recta
Dice

## Common Mistakes
# Glossary

**OTP** - One time pad
**PRNG** - Pseudo-random number generator
**mod** - The modulo operator
**Cipher** - A cryptographic algorithm or scheme
**Ciphertext** - The encrypted output from a cipher

