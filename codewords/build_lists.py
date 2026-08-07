"""Curate the codeword vocabulary into codewords/.

A codeword is read aloud over a noisy channel and then carried in someone's
head from a handover to a radio. Everything here serves that: words are
concrete and *picturable*, phonetically distinct (no shared Soundex, edit
distance >= 2), and free of NATO alphabet collisions.

Three rules decide what goes in the raw lists below.

**Picture first.** A word earns its place if a person can see the thing.
BUTTERFLY and ELEPHANT are worth more than a dozen field-guide names, because
the picture is what survives the walk to the radio. Taxonomic completeness is
not a goal; recognisability is.

**English the whole channel shares.** The lists lean on words a Nordic
speaker already owns -- through cognates (HVAL/WHALE, ELG/ELK, NARHVAL/
NARWHAL, MAMMUT/MAMMOTH, KROKODILLE/CROCODILE), through school English, or
through shared Western culture. What a Scandinavian does *not* carry is the
English name of every warbler and wrasse, so DUNLIN, GODWIT, GUDGEON and
their kind are gone, and PENGUIN, FLAMINGO, SALMON and SHARK took the room.

**Tier is priority.** Soundex is a scarce resource -- BEAR and BOAR cannot
both exist, and neither can SNOW and SWAN -- so it is spent on the word more
people can see. Each category is split into tiers, and *tier 1 of every
category is admitted before tier 2 of any of them*: the first sweep seats
SNOW, MOON and PIANO alongside BEAR and BUTTERFLY, and only then do CARIBOU
and PEONY get their turn at what is left. Within a tier, categories are
tried in the order they appear here. Losers are printed by `--show-drops`,
which is the way to check that a category is giving up the right words.

Lengths are set by the printed header, which fits 17 characters at the
tightest format (A7 with a 6-character AUTH group): MODIFIER + "-" + NOUN.
The split is 7 + 1 + 9 rather than 8 + 1 + 8, because the noun carries the
picture and the adjective only decorates it. That one character is what buys
BUTTERFLY, CROCODILE, LIGHTNING, ACCORDION and WATERFALL.
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

# Words that would read as instructions or procedure over a voice channel.
PROCEDURAL = {
    "ABORT", "AFFIRM", "BREAK", "CANCEL", "CLEAR", "CORRECT", "OUT", "OVER",
    "READY", "RELAY", "REPEAT", "ROGER", "SAY", "SEND", "STOP", "WAIT", "WILCO",
    "MESSAGE", "GROUP", "PAGE", "KEY", "PAD", "CODE", "AUTH", "DUMMY", "NUMBER",
    "RUSH", "HOLD", "NEGATIVE", "STANDBY", "COPY", "CHECK", "COUNT", "ABANDON",
}

# Tier 1: a person sees the thing the instant they hear the word.
# Tier 2: well known, still no dictionary needed.
# Tier 3: the tail -- concrete, but it gives way to anything above it.
RAW = {
"animals": [
    """
    BEAR WOLF FOX LION TIGER ELEPHANT GIRAFFE ZEBRA MONKEY GORILLA HORSE COW
    PIG SHEEP GOAT HOUND CAT MOUSE RABBIT DEER ELK REINDEER SQUIRREL HEDGEHOG
    BADGER BEAVER OTTER SEAL WALRUS WHALE DOLPHIN CAMEL KANGAROO PANDA KOALA
    HIPPO RHINO LEOPARD
    """,
    """
    JAGUAR PANTHER LYNX BISON BUFFALO DONKEY MOLE HAMSTER POLARBEAR GRIZZLY
    WOLVERINE MAMMOTH ANTELOPE GAZELLE RACCOON WEASEL FERRET SLOTH ARMADILLO
    ANTEATER PORCUPINE CHIPMUNK MEERKAT MONGOOSE HYENA JACKAL LEMUR GIBBON
    BABOON CHIMP NARWHAL ORCA BELUGA WOMBAT PLATYPUS WARTHOG LLAMA ALPACA
    PUPPY KITTEN HUSKY POODLE TERRIER GREYHOUND DALMATIAN SPANIEL BULLDOG
    STALLION DONKEY
    """,
    """
    LEMMING MUSKOX PANGOLIN CAPYBARA TAPIR MARMOT PORPOISE COUGAR OCELOT
    IMPALA GERBIL DORMOUSE MARMOSET AARDVARK OKAPI ECHIDNA QUOKKA STOAT
    MARTEN FAWN MUSTANG PIGLET SKUNK GNU BOBCAT OPOSSUM POLECAT HUMPBACK
    ROEBUCK LAMB CALF FOAL MOOSE BOAR DINGO COYOTE CHEETAH MULE PONY YAK
    """,
],
"insects": [
    """
    BUTTERFLY BEE ANT WASP FLY MOSQUITO SPIDER BEETLE LADYBIRD DRAGONFLY
    BUMBLEBEE MOTH SCORPION
    """,
    """
    HORNET FIREFLY COCKROACH CENTIPEDE MILLIPEDE TARANTULA CRICKET CICADA
    MANTIS LOCUST SILKWORM EARWIG
    """,
    """
    GLOWWORM WEEVIL APHID LOUSE TICK LARVA SCARAB WOODLOUSE MIDGE GNAT
    TERMITE FLEA MAYFLY HONEYBEE HOVERFLY HORSEFLY MEALWORM BEDBUG
    """,
],
"reptiles": [
    "SNAKE FROG TURTLE CROCODILE LIZARD DINOSAUR",
    "COBRA PYTHON VIPER TOAD IGUANA CHAMELEON ALLIGATOR ANACONDA TADPOLE",
    "TORTOISE NEWT ADDER MAMBA BULLFROG TREEFROG STEGOSAUR GECKO",
],
"birds": [
    """
    EAGLE OWL PENGUIN DUCK GOOSE CHICKEN TURKEY DOVE PIGEON SEAGULL OSTRICH
    FLAMINGO PEACOCK SPARROW FALCON RAVEN
    """,
    """
    SWAN STORK PARROT ROOSTER PELICAN VULTURE TOUCAN CUCKOO HERON CRANE
    MAGPIE PUFFIN ALBATROSS PHEASANT HAWK
    """,
    """
    QUAIL CONDOR KESTREL OSPREY BLACKBIRD STARLING SKYLARK
    FINCH CORMORANT BUZZARD LAPWING MALLARD PARTRIDGE IBIS JACKDAW
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
    "PERCH FLOUNDER HALIBUT HADDOCK MARLIN BARRACUDA CLAM",
],
"fruits": [
    "APPLE ORANGE BANANA LEMON CHERRY PEAR MELON GRAPE PEACH PINEAPPLE",
    """
    COCONUT RASPBERRY BLUEBERRY CRANBERRY APRICOT AVOCADO KIWI OLIVE LIME
    PLUM MANGO WALNUT ALMOND HAZELNUT PEANUT CHESTNUT
    """,
    "CURRANT QUINCE RAISIN RHUBARB GUAVA CASHEW FIG",
],
"vegetables": [
    "CARROT POTATO TOMATO ONION CUCUMBER CABBAGE PUMPKIN MUSHROOM GARLIC",
    "BROCCOLI SPINACH LETTUCE PEPPER CELERY RADISH BEETROOT LEEK",
    """
    PARSNIP ASPARAGUS ARTICHOKE ZUCCHINI EGGPLANT SQUASH GHERKIN LENTIL
    SHALLOT FENNEL TURNIP
    """,
],
"flowers": [
    "ROSE TULIP SUNFLOWER DAISY POPPY ORCHID LILY",
    "DANDELION BLUEBELL CLOVER DAFFODIL HYACINTH MARIGOLD PRIMROSE SNOWDROP",
    """
    CROCUS JASMINE LOTUS IRIS HEATHER BUTTERCUP FOXGLOVE THISTLE PEONY
    CARNATION GERANIUM CAMELLIA AZALEA BEGONIA PETUNIA ANEMONE COWSLIP
    HAREBELL LUPINE FUCHSIA BLOSSOM
    """,
],
"trees": [
    "OAK BIRCH SPRUCE MAPLE WILLOW PALM CEDAR",
    "PINE FIR BEECH ASPEN ROWAN ALDER HAZEL HOLLY JUNIPER LARCH LINDEN POPLAR "
    "BAMBOO CACTUS ACORN REDWOOD",
    """
    SEQUOIA BAOBAB MANGROVE MAGNOLIA CYPRESS HEMLOCK HAWTHORN DOGWOOD
    SYCAMORE EBONY ROSEWOOD FERN MOSS IVY BRACKEN REED GORSE LICHEN NETTLE
    SEDGE
    """,
],
"landforms": [
    """
    MOUNTAIN VALLEY ISLAND RIVER LAKE FOREST DESERT BEACH CANYON GLACIER
    VOLCANO WATERFALL FJORD CAVE CLIFF HILL MEADOW
    """,
    """
    SWAMP JUNGLE TUNDRA PRAIRIE OASIS GEYSER DUNE REEF HARBOR RAPIDS RAVINE
    GORGE PLATEAU SAVANNA MARSH LAGOON
    """,
    """
    MOOR ESTUARY ISTHMUS ATOLL BASIN BAY BLUFF BUTTE CAPE CAVERN CHASM COVE
    CRATER CREEK GLADE GROTTO GULLY HEADLAND HIGHLAND INLET MESA MORAINE
    SHOAL SHORE SPRING STEPPE STRAIT SUMMIT THICKET WETLAND FOOTHILL BADLANDS
    """,
],
"weather": [
    """
    SNOW STORM THUNDER RAINBOW LIGHTNING CLOUD FOG FROST HAIL SUNSHINE
    TORNADO
    """,
    """
    WIND HURRICANE AVALANCHE SNOWFLAKE BLIZZARD DRIZZLE ICICLE MONSOON TYPHOON
    CYCLONE BREEZE MIST THAW
    """,
    """
    GALE HAZE SMOG DELUGE DOWNPOUR SHOWER SQUALL TEMPEST DROUGHT FLURRY
    NIMBUS CIRRUS STRATUS OVERCAST VAPOR ZEPHYR WHIRLWIND HOARFROST
    """,
],
"celestial": [
    "MOON PLANET COMET GALAXY ECLIPSE SUNRISE SUNSET AURORA METEOR",
    "NEBULA ORBIT ASTEROID SATELLITE SUPERNOVA STARLIGHT TWILIGHT HORIZON "
    "ZENITH",
    """
    SOLSTICE EQUINOX CRESCENT COSMOS CORONA HALO DAWN DUSK STARDUST MOONBEAM
    ZODIAC PULSAR QUASAR
    """,
],
"tools": [
    """
    HAMMER AXE NAIL SCREW DRILL WRENCH SHOVEL SPADE RAKE LADDER ROPE ANCHOR
    SCISSORS NEEDLE BRUSH RULER COMPASS
    """,
    """
    CHAIN HANDSAW CHISEL CROWBAR HATCHET SICKLE SCYTHE CLAMP LEVER WEDGE
    MALLET TONGS PLIERS SPANNER
    """,
    """
    ANVIL AUGER WINCH HOIST GRINDER STAPLER SANDER SCALPEL SHEARS BELLOWS
    FORGE PULLEY RATCHET RIVET TROWEL LATHE FURNACE HACKSAW PICKAXE TOOLBOX
    SANDPAPER WORKBENCH
    """,
],
"household": [
    """
    CANDLE MIRROR PILLOW BLANKET KETTLE BUCKET CLOCK BOTTLE LAMP SPOON
    PLATE WINDOW CURTAIN
    """,
    """
    TOWEL BASKET TEAPOT LANTERN CHIMNEY CUSHION CUPBOARD UMBRELLA SUITCASE CARPET
    COMB CRADLE BARREL MATTRESS PITCHER
    """,
    """
    SAUCER TEACUP THIMBLE WHISK BUTTON RIBBON WARDROBE BOOKCASE FIREPLACE
    DOORBELL KEYHOLE STAIRCASE DOORWAY ARMCHAIR FOOTSTOOL BROOM SAUCEPAN
    DOORMAT LIGHTBULB FLOWERPOT BIRDCAGE HOURGLASS WASHBASIN
    """,
],
"vehicles": [
    "TRAIN TRACTOR BICYCLE TRUCK ROCKET BALLOON AMBULANCE",
    """
    WAGON SLEIGH SCOOTER CARAVAN GLIDER AIRSHIP ZEPPELIN TRICYCLE FIRETRUCK
    MOTORBIKE SPACESHIP AEROPLANE PARACHUTE MOPED SLEDGE
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
    TIMPANI MARIMBA PICCOLO OBOE BASSOON BUGLE HORN FIDDLE LUTE LYRE SITAR
    CHIME RATTLE RECORDER TRIANGLE
    """,
],
"buildings": [
    "CASTLE TOWER BRIDGE CHURCH WINDMILL PYRAMID COTTAGE CATHEDRAL PALACE "
    "TEMPLE",
    "FORTRESS CHAPEL ABBEY HANGAR STABLE TUNNEL FARMHOUSE WAREHOUSE "
    "MONASTERY OBELISK",
    """
    ARCHWAY BUNKER CITADEL DUNGEON GATEHOUSE GRANARY SILO SMITHY SPIRE TURRET
    VIADUCT AQUEDUCT RAMPART PAVILION PORTICO ROTUNDA CUPOLA BELFRY BEACON
    DOVECOTE MINARET PAGODA OUTPOST CISTERN KILN FOUNDRY GARRISON CAUSEWAY
    CRYPT PIER
    """,
],
"vessels": [
    "FERRY KAYAK CANOE RAFT YACHT SUBMARINE LIFEBOAT",
    """
    TANKER TRAWLER GALLEON SCHOONER LONGSHIP STEAMER GONDOLA DINGHY BARGE
    SAILBOAT ROWBOAT SPEEDBOAT HOUSEBOAT CATAMARAN
    """,
    "CLIPPER FRIGATE CORVETTE CUTTER GALLEY LAUNCH TUGBOAT TRIMARAN "
    "WHALER",
],
"colours": [
    "CRIMSON SCARLET INDIGO TURQUOISE MAGENTA VIOLET MAROON AZURE IVORY",
    "LILAC CHARCOAL OCHRE SEPIA SAFFRON BEIGE BURGUNDY MUSTARD CYAN",
    "AUBURN RUSSET SIENNA UMBER VERMILION CARMINE CERISE LAVENDER",
],
"metals": [
    "GOLD SILVER IRON COPPER BRONZE STEEL",
    "TIN ZINC NICKEL MERCURY PLATINUM TITANIUM ALUMINIUM BRASS PEWTER CHROME",
    "COBALT SOLDER INGOT ALLOY TUNGSTEN URANIUM LITHIUM BULLION NUGGET FILINGS",
],
"minerals": [
    "DIAMOND RUBY EMERALD SAPPHIRE PEARL AMBER QUARTZ MARBLE GRANITE CHALK "
    "COAL CRYSTAL",
    "JADE OPAL TOPAZ AMETHYST OBSIDIAN FLINT SLATE BASALT",
    """
    GYPSUM PUMICE GRAPHITE HEMATITE LIMESTONE SANDSTONE GARNET JASPER ONYX
    CORAL MICA
    """,
],
"fabrics": [
    "COTTON SILK WOOLLEN LINEN DENIM VELVET LEATHER",
    "CANVAS FLEECE FLANNEL TWEED SATIN TARTAN PLAID YARN",
    """
    BURLAP CASHMERE CORDUROY MUSLIN JERSEY CHIFFON BROCADE DAMASK CALICO
    GINGHAM TAFFETA SUEDE NYLON ORGANZA VELOUR
    """,
],
}

# Modifiers are chosen the same way, for the same reason: a Nordic speaker
# should read one without reaching for a dictionary. Sensory and physical
# words -- colour, size, texture, temperature, motion, light, sound, wear --
# come first; the abstract character words a schoolbook never teaches, LIMPID
# and TACIT and FRUGAL and VAGRANT, are gone.
MODIFIERS = """
RED GREEN YELLOW PURPLE WHITE BLACK BROWN GOLDEN SILVERY BRONZED COPPERY
CRIMSON SCARLET ROSY RUDDY PALE DARK BRIGHT INKY MILKY PEARLY SOOTY DUSTY
RUSTY ASHEN TAWNY REDDISH GLASSY BRASSY GRAY DUSKY SMOKY MUTED FADED

BIG TINY HUGE GIANT LITTLE BROAD NARROW TALL SHORT LONG ROUND SQUARE OVAL
CURVED BENT TWISTED FLAT THICK THIN STOUT CHUBBY LANKY SLENDER SPIRAL ZIGZAG
TAPERED ANGLED EDGED POINTED HOLLOW SOLID DENSE SPARSE

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

FIRST SECOND SINGLE DOUBLE TRIPLE LAST FINAL EARLY LATE SUDDEN SLOW SWIFT
QUICK RAPID BRISK STEADY

ROCKY SANDY MOSSY LEAFY THORNY REEDY GRASSY MARSHY MUDDY FERTILE WOODEN
EARTHEN FLINTY

HOODED VEILED CLOAKED MASKED WRAPPED NAKED BARE OPEN CLOSED LOCKED BARRED
NESTED PACKED CROWDED VACANT

SALTED BRINY SUGARED SPICY SWEET BITTER SOUR FRESH RIPE MELLOW TASTY

STURDY ROBUST BRAWNY MIGHTY STRONG FEEBLE FRAIL NIMBLE CLUMSY WOBBLY SPRINGY
LIVELY LIMBER SUPPLE

HOMELY COSY STATELY LOFTY QUAINT RUSTIC SIMPLE TIDY NEAT WILD TAMED UNTAMED
SAVAGE NOBLE ROYAL HUMBLE

BLUE PINK CREAMY HONEYED WIDE TIGHT LOOSE HEAVY FIRM SOFT HARD BLUNT SHARP
JAGGED RAGGED CLEAN DIRTY MESSY SHINY GLAD WEIRD FUNNY SILLY SCARY FLOWERY
FRUITY CRUNCHY CHEWY JUICY CRUSTY TOASTY SMOKED BAKED ROASTED BOILED HAIRY
BALD BEARDED HORNED WINGED TAILED CLAWED TOOTHY STRIPY CURLY WAVY SILKY
MARBLED GLAZED SUNKEN DROWNED WINDING WHIRLED TWIRLED HOWLING BARKING CHIRPY
SQUEAKY CREAKY FLASHY NORDIC ALPINE SUMMER WINTER MIDDAY MORNING
EVENING NIGHTLY STUFFED FILLED TWINNED PAIRED LINKED CHAINED KNITTED BLESSED
CURSED FABLED STORIED GRUFF SURLY MOODY DREAMY PERKY TENDER TOUGH HEARTY KEEN
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
    # all_kept spans every category, because a codeword's noun can come from
    # any of them -- CIVET and RIVET must not both survive just because one is
    # an animal and the other a tool.
    kept, dropped = [], []
    if all_kept is None:
        all_kept = kept
    for w in words:
        if not re.fullmatch(r"[A-Z]+", w):
            dropped.append((w, "shape"))
            continue
        if not 3 <= len(w) <= maxlen:
            # Never silently truncated: a mangled word is neither sayable nor
            # picturable, so an over-long one is reported and left out.
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

    # Modifiers and nouns keep separate phonetic namespaces -- position tells
    # the two halves apart, so one is never mistaken for the other -- but a
    # word that is both would make a codeword like SCARLET-SCARLET, so the
    # noun keeps it and the modifier gives way.
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
