"""Curate the codeword vocabulary into codewords/.

A codeword is read aloud over a noisy channel and carried in someone's head, so
words are concrete and picturable, phonetically distinct (no shared Soundex,
edit distance >= 2) and free of NATO alphabet collisions.

Rules for the raw lists:

Picture first. A word earns its place if a person can see the thing, whatever
its source: figures, folklore, food and gear count alongside natural history.
Seat the generic word (FIRE, WATER, STONE) before the species-deep one.

No field guides. The test is whether a Nordic adult already holds the picture.
A short list of words everyone owns beats a long one padded with rare
specimens. CAR, HOUSE and BOAT stay out: each is one edit from CAT, HORSE and
GOAT, the better pictures.

English the whole channel shares: cognates, school English, shared Western
culture.

Tier is priority. Soundex is scarce (BEAR and BOAR cannot both exist), so tier
1 of every category is admitted before tier 2 of any. Within a tier, categories
are tried in the order they appear. `--show-drops` prints the losers.

Lengths are set by the printed header: 17 characters at the tightest format (A7
with a 6-character AUTH group), split 7 + 1 + 9 because the noun carries the
picture.
"""
import argparse
import re
from pathlib import Path

OUT = Path(__file__).resolve().parent

# The printed header fits MODIFIER + "-" + NOUN in 17 characters.
MODIFIER_MAX = 7
NOUN_MAX = 9
HEADER_MAX = MODIFIER_MAX + 1 + NOUN_MAX

NATO = {
    "ALFA", "ALPHA", "BRAVO", "CHARLIE", "DELTA", "ECHO", "FOXTROT", "GOLF",
    "HOTEL", "INDIA", "JULIETT", "JULIET", "KILO", "LIMA", "MIKE", "NOVEMBER",
    "OSCAR", "PAPA", "QUEBEC", "ROMEO", "SIERRA", "TANGO", "UNIFORM", "VICTOR",
    "WHISKEY", "XRAY", "YANKEE", "ZULU",
}

# Words that would read as instructions over a voice channel, and words this
# system already uses about itself: a codeword is spoken on the same channel as
# the traffic it names, and "use RED-DOCUMENT" can be heard as "use the red
# document".
PROCEDURAL = {
    "ABORT", "AFFIRM", "BREAK", "CANCEL", "CLEAR", "CORRECT", "OUT", "OVER",
    "READY", "RELAY", "REPEAT", "ROGER", "SAY", "SEND", "STOP", "WAIT", "WILCO",
    "MESSAGE", "GROUP", "PAGE", "KEY", "PAD", "CODE", "AUTH", "DUMMY", "NUMBER",
    "RUSH", "HOLD", "NEGATIVE", "STANDBY", "COPY", "CHECK", "COUNT", "ABANDON",
    "POWER", "DOCUMENT", "BATTERY", "PRINTER", "PAPER", "SESSION",
}

# Tier 1: a person sees the thing the instant they hear the word.
# Tier 2: well known, still no dictionary needed.
# Tier 3: the tail -- concrete, but it gives way to anything above it.
RAW = {
"figures": [
    """
    CLOWN SANTA ROBOT PIRATE COWBOY VIKING KING PRINCESS ASTRONAUT BABY
    """,
    """
    QUEEN SAILOR DOCTOR NURSE MONK FIREMAN POLICEMAN FISHERMAN POSTMAN
    MAGICIAN JUGGLER NINJA GLADIATOR CAVEMAN CAPTAIN PILOT SOLDIER FARMER
    """,
    "BANDIT HUNTER SHEPHERD PHARAOH ACROBAT BALLERINA",
],
"folklore": [
    "GHOST DRAGON TROLL WITCH SNOWMAN SKELETON ANGEL UNICORN MERMAID",
    "VAMPIRE WEREWOLF ZOMBIE SKULL",
    """
    DEVIL KRAKEN YETI GENIE PHOENIX PEGASUS VALKYRIE CYCLOPS SCARECROW
    """,
],
"food": [
    "CHEESE PIZZA PANCAKE CHOCOLATE EGG BURGER MILK BREAD",
    """
    WAFFLE POPCORN SANDWICH ICECREAM SPAGHETTI MEATBALL KETCHUP PRETZEL
    PASTA DONUT HOTDOG TACO LASAGNE HONEY BACON SUGAR CANDY PIE BUTTER SAUSAGE
    """,
    "MARZIPAN KEBAB SALAMI JUICE BROWNIE ESPRESSO LOLLIPOP",
],
"animals": [
    """
    BEAR WOLF FOX LION TIGER ELEPHANT GIRAFFE ZEBRA MONKEY GORILLA HORSE COW
    PIG SHEEP GOAT HOUND CAT MOUSE RABBIT BUNNY DEER ELK REINDEER SQUIRREL HEDGEHOG
    BADGER BEAVER OTTER SEAL WALRUS WHALE DOLPHIN CAMEL KANGAROO PANDA KOALA
    HIPPO LEOPARD
    """,
    """
    JAGUAR PANTHER LYNX BISON BUFFALO DONKEY MOLE HAMSTER POLARBEAR GRIZZLY
    WOLVERINE MAMMOTH ANTELOPE RACCOON WEASEL FERRET SLOTH
    PORCUPINE CHIPMUNK HYENA JACKAL
    BABOON CHIMP NARWHAL ORCA WARTHOG LLAMA
    PUPPY KITTEN POODLE TERRIER GREYHOUND DALMATIAN BULLDOG STALLION
    """,
    """
    LEMMING MUSKOX FAWN PIGLET SKUNK LAMB CALF FOAL BOAR COYOTE CHEETAH
    MULE PONY
    """,
],
"insects": [
    """
    BUTTERFLY BEE ANT WASP FLY MOSQUITO SPIDER BEETLE LADYBIRD DRAGONFLY
    BUMBLEBEE MOTH SCORPION
    """,
    """
    HORNET FIREFLY COCKROACH CENTIPEDE MILLIPEDE TARANTULA CRICKET
    MANTIS LOCUST CATERPILLAR
    """,
    """
    GLOWWORM TICK TERMITE FLEA HORSEFLY BEDBUG WORM
    """,
],
"reptiles": [
    "SNAKE FROG TURTLE CROCODILE LIZARD DINOSAUR",
    "COBRA PYTHON VIPER TOAD IGUANA CHAMELEON ALLIGATOR ANACONDA TADPOLE",
    "TORTOISE NEWT BULLFROG GECKO",
],
"birds": [
    """
    EAGLE OWL PENGUIN DUCK GOOSE CHICKEN DOVE PIGEON SEAGULL OSTRICH
    FLAMINGO PEACOCK SPARROW FALCON RAVEN FEATHER
    """,
    """
    SWAN STORK PARROT ROOSTER VULTURE TOUCAN CUCKOO HERON CRANE
    MAGPIE PUFFIN ALBATROSS HAWK
    """,
    """
    BLACKBIRD WOODPECKER NIGHTINGALE
    """,
],
"fish": [
    """
    SHARK SALMON TUNA TROUT HERRING OCTOPUS JELLYFISH STARFISH CRAB LOBSTER
    SHRIMP GOLDFISH
    """,
    """
    CODFISH EEL SWORDFISH SQUID OYSTER MUSSEL STINGRAY PIRANHA MACKEREL
    SARDINE ANCHOVY SEAHORSE
    """,
    "PERCH HALIBUT HADDOCK CLAM",
],
"clothing": [
    "HELMET SHOE JACKET GLOVE SOCK CROWN",
    """
    MASK SCARF BOOT SWEATER TROUSERS JEANS PYJAMAS KIMONO PARKA BELT SHORTS
    NECKTIE SANDAL TURBAN GLASSES TOPHAT SKI SHIRT DRESS APRON MITTENS
    """,
    "HOODIE EYEPATCH BALACLAVA MONOCLE RAINCOAT",
],
"gear": [
    "SWORD SHIELD ARROW FLAG",
    """
    FOOTBALL BACKPACK TROPHY MEDAL HARPOON SKATE SNOWBOARD PADDLE CATAPULT
    LASSO SNORKEL HANDBAG WALLET
    """,
    "CROSSBOW DAGGER DARTBOARD MAP",
],
"toys": [
    "BALL TEDDYBEAR",
    "DOLL BUBBLE BOOMERANG",
    "PUPPET DOLLHOUSE TREEHOUSE KITE DOMINO JOKER",
],
"festive": [
    "HEART",
    "CIRCUS CAROUSEL CAMPFIRE TENT BONFIRE FIREWORK",
    "TRAPEZE STILTS CONFETTI MISTLETOE SNOWGLOBE STOCKING",
],
"fruits": [
    "APPLE ORANGE BANANA LEMON CHERRY PEAR MELON GRAPE PEACH PINEAPPLE",
    """
    COCONUT RASPBERRY BLUEBERRY CRANBERRY APRICOT AVOCADO KIWI OLIVE LIME
    PLUM MANGO WALNUT ALMOND HAZELNUT PEANUT CHESTNUT
    """,
    "CURRANT RAISIN RHUBARB CASHEW FIG",
],
"vegetables": [
    "CARROT POTATO TOMATO ONION CUCUMBER CABBAGE PUMPKIN MUSHROOM GARLIC",
    "BROCCOLI SPINACH LETTUCE PEPPER CELERY RADISH BEETROOT LEEK",
    """
    ASPARAGUS ZUCCHINI SQUASH GHERKIN LENTIL TURNIP
    """,
],
"flowers": [
    "FLOWER ROSE TULIP SUNFLOWER DAISY POPPY ORCHID LILY",
    "DANDELION BLUEBELL CLOVER DAFFODIL HYACINTH MARIGOLD PRIMROSE SNOWDROP",
    """
    CROCUS JASMINE LOTUS IRIS HEATHER BUTTERCUP THISTLE BLOSSOM
    """,
],
"trees": [
    "OAK LEAF WOOD BIRCH SPRUCE MAPLE WILLOW PALM CEDAR",
    "PINE FIR BEECH ASPEN ROWAN ALDER HAZEL HOLLY JUNIPER LARCH LINDEN POPLAR "
    "BAMBOO CACTUS ACORN REDWOOD",
    """
    FERN MOSS IVY REED NETTLE STUMP BRANCH
    """,
],
"elements": [
    # The words a child draws first.
    "FIRE WATER ICE STONE GRASS",
    "FLAME EMBER PUDDLE RIPPLE",
    "SMOKE SAND DUST STEAM",
],
"landforms": [
    """
    MOUNTAIN VALLEY ISLAND RIVER LAKE FOREST DESERT BEACH CANYON GLACIER
    VOLCANO WATERFALL FJORD CAVE CLIFF HILL MEADOW PATH GARDEN
    """,
    """
    SWAMP JUNGLE TUNDRA OASIS GEYSER DUNE REEF HARBOR RAPIDS MARSH LAGOON
    FOOTPRINT HAYSTACK
    """,
    """
    BAY CRATER CREEK ICEBERG SHORE SPRING SUMMIT
    """,
],
"weather": [
    """
    RAIN SNOW STORM THUNDER RAINBOW LIGHTNING CLOUD FOG FROST HAIL SUNSHINE
    TORNADO
    """,
    """
    WIND HURRICANE AVALANCHE SNOWFLAKE BLIZZARD DRIZZLE ICICLE MONSOON TYPHOON
    CYCLONE BREEZE MIST THAW
    """,
    """
    DOWNPOUR SHOWER DROUGHT WHIRLWIND
    """,
],
"celestial": [
    "MOON STAR PLANET COMET GALAXY ECLIPSE SUNRISE SUNSET AURORA METEOR",
    "NEBULA ORBIT ASTEROID SATELLITE STARLIGHT TWILIGHT HORIZON",
    """
    CRESCENT HALO DAWN DUSK MOONBEAM
    """,
],
"tools": [
    """
    HAMMER AXE NAIL SCREW DRILL WRENCH SHOVEL SPADE RAKE LADDER ROPE ANCHOR
    SCISSORS NEEDLE BRUSH RULER COMPASS KNIFE
    """,
    """
    CHAIN TELESCOPE HANDSAW CHISEL CROWBAR HATCHET SICKLE SCYTHE CLAMP LEVER WEDGE
    MALLET TONGS PLIERS SPANNER
    """,
    """
    ANVIL STAPLER SHEARS PADLOCK PULLEY PICKAXE TOOLBOX SANDPAPER WORKBENCH
    """,
],
"household": [
    """
    CANDLE MIRROR PILLOW BLANKET KETTLE BUCKET CLOCK BOTTLE LAMP SPOON
    PLATE WINDOW CURTAIN JAR SHELF PENCIL NOTEBOOK ENVELOPE NEWSPAPER
    """,
    """
    TOWEL BASKET CAMERA TEAPOT LANTERN CHIMNEY CUSHION CUPBOARD UMBRELLA SUITCASE CARPET
    COMB CRADLE BARREL MATTRESS PITCHER DRAWER POSTCARD
    """,
    """
    TEACUP WHISK BUTTON RIBBON WARDROBE BOOKCASE FIREPLACE
    DOORBELL KEYHOLE STAIRCASE DOORWAY ARMCHAIR BROOM SAUCEPAN
    DOORMAT LIGHTBULB FLOWERPOT BIRDCAGE HOURGLASS
    """,
],
"vehicles": [
    "TRAIN TRACTOR BICYCLE TRUCK ROCKET BALLOON AMBULANCE",
    """
    WAGON SLEIGH SCOOTER CARAVAN GLIDER AIRSHIP ZEPPELIN TRICYCLE FIRETRUCK
    MOTORBIKE SPACESHIP AEROPLANE PARACHUTE MOPED SLEDGE ENGINE
    """,
    "CHARIOT CARRIAGE SNOWPLOW BULLDOZER BIPLANE TOBOGGAN TANDEM FORKLIFT "
    "MINIBUS SUBWAY TROLLEY HANDCART RICKSHAW LIMOUSINE",
],
"instruments": [
    "GUITAR PIANO DRUM VIOLIN TRUMPET FLUTE HARP CELLO ORGAN SAXOPHONE "
    "ACCORDION",
    """
    BANJO CLARINET TROMBONE HARMONICA BAGPIPE MANDOLIN UKULELE XYLOPHONE
    BELL GONG CYMBAL WHISTLE TUBA
    """,
    """
    PICCOLO BASSOON BUGLE HORN FIDDLE LUTE SITAR
    CHIME RATTLE RECORDER TRIANGLE
    """,
],
"buildings": [
    "BARN FENCE CASTLE TOWER BRIDGE CHURCH WINDMILL PYRAMID COTTAGE "
    "CATHEDRAL PALACE TEMPLE",
    "FORTRESS CHAPEL HANGAR STABLE TUNNEL BAKERY FARMHOUSE WAREHOUSE "
    "LIGHTHOUSE SCHOOL",
    """
    BUNKER DUNGEON SILO BEACON OUTPOST PIER
    """,
],
"vessels": [
    "FERRY KAYAK CANOE RAFT YACHT SUBMARINE LIFEBOAT",
    """
    TANKER TRAWLER GALLEON LONGSHIP STEAMER GONDOLA BARGE
    SAILBOAT ROWBOAT SPEEDBOAT HOUSEBOAT
    """,
    "TUGBOAT",
],
"colours": [
    "CRIMSON SCARLET INDIGO TURQUOISE MAGENTA VIOLET MAROON IVORY",
    "LILAC CHARCOAL BEIGE BURGUNDY MUSTARD CYAN",
    "LAVENDER",
],
"metals": [
    "GOLD SILVER IRON COPPER BRONZE STEEL",
    "TIN ZINC NICKEL MERCURY PLATINUM TITANIUM ALUMINIUM BRASS CHROME",
    "URANIUM LITHIUM NUGGET",
],
"minerals": [
    "DIAMOND RUBY EMERALD SAPPHIRE PEARL AMBER QUARTZ MARBLE GRANITE CHALK "
    "COAL CRYSTAL",
    "JADE OPAL TOPAZ AMETHYST OBSIDIAN FLINT SLATE",
    """
    GRAPHITE LIMESTONE SANDSTONE ONYX CORAL
    """,
],
"fabrics": [
    "COTTON WOOLLEN LINEN DENIM VELVET LEATHER",
    "CANVAS FLEECE FLANNEL TWEED TARTAN PLAID YARN",
    """
    CASHMERE CORDUROY JERSEY SUEDE NYLON
    """,
],
}

# Modifiers follow the same rule: sensory and physical words a Nordic speaker
# reads without a dictionary. Every modifier is eventually printed against
# every noun, people included, so one is disqualified if a pair it makes with a
# person is one an operator would not read aloud to a stranger.
MODIFIERS = """
RED GREEN YELLOW PURPLE WHITE BLACK BROWN GOLDEN SILVERY BRONZED COPPERY
CRIMSON SCARLET ROSY RUDDY PALE DARK BRIGHT INKY MILKY PEARLY SOOTY DUSTY
RUSTY ASHEN TAWNY REDDISH GLASSY BRASSY GRAY DUSKY SMOKY MUTED FADED

BIG TINY HUGE GIANT LITTLE BROAD NARROW TALL SHORT LONG ROUND SQUARE OVAL
CURVED BENT TWISTED FLAT THICK THIN STOUT LANKY SLENDER SPIRAL ZIGZAG
TAPERED ANGLED EDGED POINTED HOLLOW SOLID DENSE 

ROUGH SMOOTH SILKEN VELVETY WOOLLY BUMPY SPIKY PRICKLY SLIMY STICKY GLOSSY
FLUFFY FURRY SCALY WAXEN GRAINY KNOTTED RIBBED RUGGED CHALKY GNARLED COARSE
CRISP FLAKY SHAGGY TUFTED PEBBLED RIPPLED GROOVED FLUTED LACED

FROZEN FROSTY ICY CHILLY WARM BOILING MOLTEN STEAMY SUNNY RAINY SNOWY WINDY
MISTY FOGGY STORMY CLOUDY BALMY HUMID PARCHED GLACIAL WINTRY AUTUMN ARCTIC
POLAR TIDAL DAMP DRY DEWY WET SODDEN SOAKED WATERY FOAMING

FLYING RUNNING JUMPING DANCING RESTING ROAMING ROLLING SOARING DIVING GLIDING
HOPPING SWAYING LEAPING FLOWING RISING FALLEN TURNING TUMBLED DRIFTED SWIRLED
FLOATED CIRCLED

GLOWING SHINING SUNLIT STARLIT MOONLIT DIMMED SHADOWY TWILIT DAWNING BURNING
CHARRED GILDED BLAZING STARRY SHADED

BROKEN MENDED RUSTED WORN ANCIENT OLDEN AGED LOST HIDDEN SECRET EMPTY LOADED
CRACKED SEALED BURIED CARVED FORGED WOVEN BRAIDED PLEATED FOLDED TANGLED
PATCHED SCRAPED STAINED PAINTED WASHED WILTED CURLED COILED CLIPPED

SPOTTED STRIPED DAPPLED MOTTLED FLECKED DOTTED CHECKED PLAIN ORNATE TINTED

SILENT HUSHED MUFFLED ECHOING RINGING HUMMING BUZZING ROARING RASPING GUSTY

HAPPY MERRY JOLLY GRUMPY SLEEPY ANGRY CALM QUIET NOISY LOUD BRAVE CLEVER LAZY
EAGER HUNGRY THIRSTY GENTLE FIERCE TIMID PROUD WEARY TIRED CURIOUS GLOOMY
LONELY WICKED SPOOKY LUCKY CHEERY HAUNTED

EASTERN WESTERN INLAND UPLAND UPPER LOWER INNER OUTER DISTANT NEARBY DEEP
SHALLOW HIGH STEEP SLOPING LEVEL UNEVEN SEASIDE COASTAL

FIRST SECOND SINGLE DOUBLE TRIPLE LAST FINAL EARLY LATE SUDDEN SLOW
QUICK RAPID BRISK STEADY

ROCKY SANDY MOSSY LEAFY THORNY REEDY GRASSY MARSHY MUDDY FERTILE WOODEN
EARTHEN FLINTY

HOODED VEILED CLOAKED MASKED WRAPPED OPEN CLOSED LOCKED BARRED
NESTED PACKED CROWDED VACANT

SALTED BRINY SUGARED SPICY SWEET BITTER SOUR FRESH RIPE MELLOW 

STURDY ROBUST BRAWNY MIGHTY STRONG FEEBLE FRAIL NIMBLE CLUMSY WOBBLY SPRINGY
LIVELY LIMBER SUPPLE

HOMELY COSY STATELY LOFTY QUAINT RUSTIC SIMPLE TIDY NEAT WILD TAMED UNTAMED
SAVAGE NOBLE ROYAL HUMBLE

BLUE PINK HONEYED WIDE TIGHT LOOSE HEAVY FIRM SOFT HARD BLUNT SHARP
JAGGED RAGGED CLEAN MESSY SHINY GLAD WEIRD FUNNY SILLY SCARY FLOWERY
CRUNCHY CHEWY CRUSTY TOASTY SMOKED BAKED ROASTED BOILED HAIRY
BALD BEARDED HORNED WINGED TAILED CLAWED TOOTHY STRIPY CURLY WAVY SILKY
MARBLED GLAZED SUNKEN DROWNED WINDING WHIRLED TWIRLED HOWLING BARKING CHIRPY
SPARKLY CAPED SQUEAKY CREAKY FLASHY NORDIC ALPINE SUMMER WINTER MIDDAY MORNING
EVENING NIGHTLY STUFFED FILLED TWINNED PAIRED LINKED CHAINED KNITTED BLESSED
CURSED FABLED STORIED GRUFF SURLY MOODY DREAMY TENDER TOUGH HEARTY KEEN
SLICK GRUBBY GRIMY BOLD HANGING PERCHED NESTING HUNTING GRAZING FEEDING
PADDLED ROWING SAILING HEATED COOLED CHILLED WARMED BLOWN BLASTED CRUMBLY
"""


def soundex(word: str) -> str:
    codes = {**dict.fromkeys("BFPV", "1"), **dict.fromkeys("CGJKQSXZ", "2"),
             **dict.fromkeys("DT", "3"), "L": "4",
             **dict.fromkeys("MN", "5"), "R": "6"}
    first, rest = word[0], word[1:]
    out, prev = first, codes.get(first, "")
    for ch in rest:
        code = codes.get(ch, "")
        if code and code != prev:
            out += code
        if ch not in "HW":
            prev = code
    return (out + "000")[:4]


def edit_distance(a: str, b: str) -> int:
    if abs(len(a) - len(b)) > 1:
        return 2
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def clean(raw: str) -> list[str]:
    return [w for w in raw.split() if w]


def filter_list(words, taken_soundex, taken_words, maxlen, all_kept=None,
                forbidden=frozenset()):
    # all_kept spans every category, since a noun can come from any: CIVET and
    # RIVET must not both survive.
    kept, dropped = [], []
    if all_kept is None:
        all_kept = kept
    for w in words:
        if not re.fullmatch(r"[A-Z]+", w):
            dropped.append((w, "shape"))
            continue
        if not 3 <= len(w) <= maxlen:
            # An over-long word is reported and left out, never truncated.
            dropped.append((w, f"too long (>{maxlen})" if len(w) > maxlen
                            else "too short (<3)"))
            continue
        if w in NATO or w in PROCEDURAL:
            dropped.append((w, "reserved"))
            continue
        if w in forbidden:
            dropped.append((w, "already a noun"))
            continue
        if w in taken_words:
            dropped.append((w, "duplicate"))
            continue
        sx = soundex(w)
        if sx in taken_soundex:
            dropped.append((w, f"soundex~{taken_soundex[sx]}"))
            continue
        close = next((k for k in all_kept if edit_distance(w, k) < 2), None)
        if close:
            dropped.append((w, f"editdist~{close}"))
            continue
        kept.append(w)
        if all_kept is not kept:
            all_kept.append(w)
        taken_words.add(w)
        taken_soundex[sx] = w
    return kept, dropped


def build_nouns():
    """Admit every category's tier 1, then every category's tier 2, and so on."""
    taken_soundex, taken_words, all_nouns = {}, set(), []
    kept = {name: [] for name in RAW}
    dropped = {name: [] for name in RAW}
    for tier in range(max(len(tiers) for tiers in RAW.values())):
        for name, tiers in RAW.items():
            if tier >= len(tiers):
                continue
            k, d = filter_list(clean(tiers[tier]), taken_soundex, taken_words,
                               NOUN_MAX, all_nouns)
            kept[name] += k
            dropped[name] += [(w, why, tier + 1) for w, why in d]
    return kept, dropped, taken_words, all_nouns


def main():
    ap = argparse.ArgumentParser(description="Rebuild the codeword vocabulary.")
    ap.add_argument("--show-drops", action="store_true",
                    help="print every rejected word and why it lost")
    args = ap.parse_args()

    (OUT / "nouns").mkdir(parents=True, exist_ok=True)
    kept, dropped, noun_words, all_nouns = build_nouns()
    for name, words in kept.items():
        (OUT / "nouns" / f"{name}.txt").write_text("\n".join(sorted(words)) + "\n")

    # Modifiers and nouns keep separate phonetic namespaces, since position
    # tells them apart, but a word in both would allow SCARLET-SCARLET, so the
    # noun keeps it.
    mods, mod_drops = filter_list(clean(MODIFIERS), {}, set(), MODIFIER_MAX,
                                  forbidden=noun_words)
    (OUT / "modifiers.txt").write_text("\n".join(sorted(mods)) + "\n")

    report = [(name, kept[name], dropped[name]) for name in RAW]
    report.append(("modifiers", mods, [(w, why, 0) for w, why in mod_drops]))
    for name, words, drops in report:
        print(f"{name:14s} kept={len(words):4d} dropped={len(drops):3d}")
        if args.show_drops:
            for word, why, tier in drops:
                mark = f"T{tier} " if tier else ""
                print(f"                 - {mark}{word:12s} {why}")

    longest = f"{max(mods, key=len)}-{max(all_nouns, key=len)}"
    print(f"\nmodifiers={len(mods)}  nouns={len(all_nouns)}  "
          f"combinations={len(mods) * len(all_nouns):,}")
    print(f"longest codeword={longest} ({len(longest)}/{HEADER_MAX} header chars)")


if __name__ == "__main__":
    main()
